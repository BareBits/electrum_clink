"""Unit tests for the listener's readiness gate and relay watchdog.

Covers two silent-listener failure modes found in the field:

  * ``run()`` used to gate on the raw config relay list (``relay_url``), so a
    wallet whose offers are all pinned to custom relays — but whose global
    ``NOSTR_RELAYS`` is empty — never started the listener at all, while offer
    creation kept succeeding.
  * ``Manager.connect()`` silently drops relays it could not reach and never
    retries them, while the drift check compared the *requested* relay set —
    a pinned relay with a transient outage at (re)connect time stayed missing
    (its offers unpayable) for the rest of the session.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

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
