"""Unit tests for the listener's readiness gate and relay watchdog.

Covers silent-listener failure modes found in the field:

  * ``run()`` used to gate on the raw config relay list (``relay_url``), so a
    wallet whose offers are all pinned to custom relays — but whose global
    ``NOSTR_RELAYS`` is empty — never started the listener at all, while offer
    creation kept succeeding.
  * ``Manager.connect()`` silently drops relays it could not reach and never
    retries them, while the drift check compared the *requested* relay set —
    a pinned relay with a transient outage at (re)connect time stayed missing
    (its offers unpayable) for the rest of the session.
  * A relay connection that died mid-session (heartbeat-detected close,
    crashed receive loop) stayed in ``manager.relays`` looking healthy, so the
    membership watchdog never re-attached it (``prune_dead_relays``).
  * A half-open TCP connection delivered nothing while passing every state
    check — the long-uptime "noffers stop being answered until restart" bug —
    which only an end-to-end round-trip can catch (``ping_listener``).
"""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from electrum_aionostr.event import Event
from electrum_aionostr.key import PrivateKey

from clink.clink_plugin import ClinkServer
from clink.offers import Offer


def _server_shell() -> ClinkServer:
    from electrum.logging import Logger
    server = ClinkServer.__new__(ClinkServer)
    Logger.__init__(server)
    return server


# --- listener_prerequisites_met ---------------------------------------------

def _gate_server(*, offers: List[Offer], nostr_relays: List[str],
                 connected: bool = True, lnworker: bool = True) -> ClinkServer:
    server = _server_shell()
    server.config = SimpleNamespace(
        CLINK_RELAY="",
        get_nostr_relays=lambda: nostr_relays,
    )
    server.offers = SimpleNamespace(list=lambda: offers)
    server._relay_selection = None
    server._relay_selection_at = 0.0
    server.wallet = SimpleNamespace(
        network=SimpleNamespace(is_connected=lambda: connected),
        lnworker=SimpleNamespace() if lnworker else None,
    )
    return server


def test_ready_with_config_relays_and_no_offers() -> None:
    assert _gate_server(offers=[], nostr_relays=["wss://r1"]).listener_prerequisites_met()


def test_custom_relay_offer_alone_is_enough() -> None:
    """The regression: empty NOSTR_RELAYS + an offer pinned to a custom relay
    must still bring the listener up (the old relay_url gate never did)."""
    offer = Offer(offer_id="o1", relay="wss://custom.example", relay_custom=True)
    server = _gate_server(offers=[offer], nostr_relays=[])
    assert server.listener_prerequisites_met()
    assert server.listen_relay_urls() == ["wss://custom.example"]


def test_not_ready_with_nothing_to_listen_on() -> None:
    assert not _gate_server(offers=[], nostr_relays=[]).listener_prerequisites_met()


def test_not_ready_when_network_down_or_no_lnworker() -> None:
    assert not _gate_server(
        offers=[], nostr_relays=["wss://r1"], connected=False).listener_prerequisites_met()
    assert not _gate_server(
        offers=[], nostr_relays=["wss://r1"], lnworker=False).listener_prerequisites_met()


# --- ensure_listener_relays ---------------------------------------------------

class FakeManager:
    def __init__(self, connected_urls: List[str], *, connected: bool = True) -> None:
        self.connected = connected
        self.relays = [SimpleNamespace(url=u) for u in connected_urls]
        self.updates: List[List[str]] = []

    async def update_relays(self, urls: Any) -> None:
        self.updates.append(list(urls))


def _watchdog_server(manager: Optional[FakeManager],
                     listen_urls: List[str]) -> ClinkServer:
    server = _server_shell()
    server.manager = manager
    server.listen_relay_urls = lambda: listen_urls  # type: ignore[method-assign]
    return server


def test_missing_relay_triggers_update_with_full_set() -> None:
    manager = FakeManager(["wss://a.example"])
    server = _watchdog_server(manager, ["wss://a.example", "wss://b.example"])
    asyncio.run(server.ensure_listener_relays())
    assert manager.updates == [["wss://a.example", "wss://b.example"]]


def test_all_relays_present_is_a_noop() -> None:
    manager = FakeManager(["wss://a.example", "wss://b.example"])
    server = _watchdog_server(manager, ["wss://a.example", "wss://b.example"])
    asyncio.run(server.ensure_listener_relays())
    assert manager.updates == []


def test_url_normalization_does_not_thrash() -> None:
    # Manager stores normalized urls (lowercased, no trailing slash); the
    # desired list may carry the raw user-typed form. Equal after
    # normalization -> no reconnect attempt.
    manager = FakeManager(["wss://a.example"])
    server = _watchdog_server(manager, ["wss://A.example/"])
    asyncio.run(server.ensure_listener_relays())
    assert manager.updates == []


def test_disconnected_or_absent_manager_is_a_noop() -> None:
    server = _watchdog_server(None, ["wss://a.example"])
    asyncio.run(server.ensure_listener_relays())  # must not raise

    manager = FakeManager([], connected=False)
    server = _watchdog_server(manager, ["wss://a.example"])
    asyncio.run(server.ensure_listener_relays())
    assert manager.updates == []


# --- prune_dead_relays / ping_listener ----------------------------------------

class FakeRelay:
    """A relay stub carrying exactly the connection state the watchdog reads.

    ``echo=True`` makes ``add_event`` behave like a live round-trip: the ping
    just published comes straight back and resolves its waiter (``server`` is
    wired in by the harness). ``echo=False`` is the half-open connection: the
    send "succeeds" (the kernel would buffer it) but nothing ever returns.
    """

    def __init__(self, url: str, *, connected: bool = True, ws_closed: bool = False,
                 task_done: bool = False, echo: bool = True,
                 send_raises: bool = False) -> None:
        self.url = url
        self.connected = connected
        self.ws = SimpleNamespace(closed=ws_closed)
        self.receive_task = SimpleNamespace(done=lambda: task_done)
        self.echo = echo
        self.send_raises = send_raises
        self.closed = False
        self.server: Optional[ClinkServer] = None
        self.sent: List[Dict[str, Any]] = []

    async def close(self) -> None:
        self.closed = True

    async def add_event(self, event: Dict[str, Any], check_response: bool = False) -> None:
        if self.send_raises:
            raise ConnectionResetError("boom")
        self.sent.append(event)
        if self.echo and self.server is not None:
            waiter = self.server._pending_pings.get(event["id"])
            if waiter is not None:
                waiter.set()


class FakeRelayManager:
    def __init__(self, relays: List[FakeRelay], *, connected: bool = True) -> None:
        self.connected = connected
        self.relays: List[FakeRelay] = relays
        self.updates: List[List[str]] = []

    async def update_relays(self, urls: Any) -> None:
        self.updates.append(list(urls))


def _ping_server(relays: List[FakeRelay], *, connected: bool = True) -> ClinkServer:
    server = _server_shell()
    sk = PrivateKey()
    server.private_key = sk
    server.pubkey_hex = sk.public_key.hex()
    server._pending_pings = {}
    server.recent_activity = deque(maxlen=50)
    server.manager = FakeRelayManager(relays, connected=connected)
    server.listen_relay_urls = lambda: [r.url for r in relays]  # type: ignore[method-assign]
    for relay in relays:
        relay.server = server
    return server


def test_prune_drops_only_observably_dead_relays() -> None:
    healthy = FakeRelay("wss://ok.example")
    ws_dead = FakeRelay("wss://ws-dead.example", ws_closed=True)
    task_dead = FakeRelay("wss://task-dead.example", task_done=True)
    disconnected = FakeRelay("wss://disc.example", connected=False)
    server = _ping_server([healthy, ws_dead, task_dead, disconnected])
    asyncio.run(server.prune_dead_relays())
    assert server.manager.relays == [healthy]
    assert not healthy.closed
    assert ws_dead.closed and task_dead.closed and disconnected.closed


def test_prune_is_a_noop_when_all_healthy() -> None:
    relays = [FakeRelay("wss://a.example"), FakeRelay("wss://b.example")]
    server = _ping_server(relays)
    asyncio.run(server.prune_dead_relays())
    assert server.manager.relays == relays


def test_ping_all_relays_echo() -> None:
    relays = [FakeRelay("wss://a.example"), FakeRelay("wss://b.example")]
    server = _ping_server(relays)
    assert asyncio.run(server.ping_listener(timeout=1)) is True
    assert server.manager.relays == relays
    assert not any(r.closed for r in relays)
    assert server._pending_pings == {}  # nothing left behind


def test_ping_missing_echo_drops_and_reattaches_only_that_relay() -> None:
    """The deaf-relay case: the send goes into the void and nothing comes
    back. That relay (and only that relay) must be dropped and the listener
    re-attach pass must run, while the healthy relay's service is untouched."""
    good = FakeRelay("wss://good.example")
    deaf = FakeRelay("wss://deaf.example", echo=False)
    server = _ping_server([good, deaf])
    assert asyncio.run(server.ping_listener(timeout=0.05)) is False
    assert server.manager.relays == [good]
    assert deaf.closed and not good.closed
    # the re-attach pass ran with the full desired set (sorted, as ensure_
    # listener_relays passes it)
    assert server.manager.updates == [["wss://deaf.example", "wss://good.example"]]
    assert server._pending_pings == {}


def test_ping_send_failure_counts_as_dead() -> None:
    broken = FakeRelay("wss://broken.example", send_raises=True)
    server = _ping_server([broken])
    assert asyncio.run(server.ping_listener(timeout=0.05)) is False
    assert broken.closed
    assert server.manager.relays == []


def test_ping_with_no_manager_or_relays_is_a_noop() -> None:
    server = _ping_server([])
    assert asyncio.run(server.ping_listener(timeout=0.05)) is True

    server = _ping_server([FakeRelay("wss://a.example")], connected=False)
    assert asyncio.run(server.ping_listener(timeout=0.05)) is True


def test_handle_requests_raises_when_event_stream_ends() -> None:
    """A live subscription ending on its own is always wrong (found via the
    rig: a poisoned end-of-stream sentinel ended the async-for and left run()'s
    taskgroup humming with no listener). handle_requests must raise so run()
    rebuilds the whole listener instead of dying silently."""
    server = _ping_server([])

    class _EndingManager:
        async def get_events(self, query: Any, single_event: bool = False,
                             only_stored: bool = False):
            return
            yield  # pragma: no cover  (makes this an async generator)

    server.manager = _EndingManager()
    with pytest.raises(RuntimeError, match="ended unexpectedly"):
        asyncio.run(server.handle_requests())


def test_dispatch_consumes_ping_echo() -> None:
    """A ping echoed back through the subscription resolves its waiter and is
    swallowed before any request processing (the shell has no offers/reserver
    wired — reaching them would raise)."""
    server = _ping_server([])
    ping = server._make_listener_ping()
    waiter = asyncio.Event()
    server._pending_pings[ping.id] = waiter
    # what the relay hands back is a re-parsed event, not the same object
    echoed = Event.from_json(ping.to_json_object())
    asyncio.run(server._dispatch(echoed))
    assert waiter.is_set()
    assert ping.id not in server._pending_pings
