"""Unit tests for the relay payability probe.

Covers two independently-testable layers with no network:

  * ``select_payable_relay`` — the candidate-ordering / fallback policy, driven
    by a fake ``probe`` coroutine.
  * ``_run_probe`` — the connect -> subscribe -> publish -> read-back ordering,
    driven by an in-memory fake relay hub that only delivers an event to a
    *live* subscriber (mirroring how real relays drop ephemeral events with no
    current subscriber — the exact failure the probe must detect).

Following the repo convention (no pytest-asyncio), each test drives its
coroutine with ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

import pytest

from clink.relay_probe import (
    PROBE_CONTENT_PREFIX,
    ProbeResult,
    ProbeStatus,
    _run_probe,
    normalize_relay_url,
    select_payable_relay,
)


# --- normalize_relay_url: user-supplied relay validation ------------------

@pytest.mark.parametrize("url", [
    "wss://myrelay.com:7777",
    "wss://myrelay.com",
    "ws://127.0.0.1:8088",          # plain ws for local/regtest relays
    "wss://relay.example.org/nostr",
])
def test_normalize_relay_url_accepts_wellformed(url: str) -> None:
    assert normalize_relay_url(url) == url


def test_normalize_relay_url_strips_whitespace() -> None:
    assert normalize_relay_url("  wss://myrelay.com:7777 \n") == "wss://myrelay.com:7777"


@pytest.mark.parametrize("url", [
    "",
    "   ",
    "myrelay.com",                  # no scheme
    "https://myrelay.com",          # wrong scheme
    "wss://",                       # no host
    "wss://myrelay.com:notaport",   # malformed port
    "wss://my relay.com:7777",      # embedded whitespace
])
def test_normalize_relay_url_rejects_malformed(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_relay_url(url)


# --- select_payable_relay: ordering + fallback policy --------------------

def _probe_returning(mapping):
    async def probe(relay: str) -> ProbeResult:
        return ProbeResult(relay, mapping.get(relay, ProbeStatus.NO_READBACK))
    return probe


def test_selects_first_when_first_is_payable() -> None:
    cands = ["wss://a", "wss://b", "wss://c"]
    probe = _probe_returning({"wss://a": ProbeStatus.OK, "wss://b": ProbeStatus.OK})
    sel = asyncio.run(select_payable_relay(cands, probe=probe))
    assert sel.ok and sel.relay == "wss://a"
    assert [r.relay for r in sel.results] == cands  # every candidate reported


def test_skips_dead_relays_and_picks_first_working() -> None:
    # Mirrors the real bug: the default first relay is unusable, a later one works.
    cands = ["wss://relay.getalby.com/v1", "wss://nos.lol", "wss://relay.damus.io"]
    probe = _probe_returning({
        "wss://relay.getalby.com/v1": ProbeStatus.NO_READBACK,
        "wss://nos.lol": ProbeStatus.OK,
        "wss://relay.damus.io": ProbeStatus.OK,
    })
    sel = asyncio.run(select_payable_relay(cands, probe=probe))
    assert sel.ok and sel.relay == "wss://nos.lol"


def test_all_failing_returns_first_candidate_not_ok() -> None:
    cands = ["wss://a", "wss://b"]
    probe = _probe_returning({})  # everything NO_READBACK
    sel = asyncio.run(select_payable_relay(cands, probe=probe))
    assert not sel.ok
    assert sel.relay == "wss://a"          # still usable so a noffer can be built
    assert len(sel.failures()) == 2


def test_empty_candidates() -> None:
    called = False

    async def probe(relay: str) -> ProbeResult:  # pragma: no cover - must not run
        nonlocal called
        called = True
        return ProbeResult(relay, ProbeStatus.OK)

    sel = asyncio.run(select_payable_relay([], probe=probe))
    assert not sel.ok and sel.relay == "" and sel.results == []
    assert not called


def test_blank_candidates_are_ignored() -> None:
    probe = _probe_returning({"wss://b": ProbeStatus.OK})
    sel = asyncio.run(select_payable_relay(["", "  ", "wss://b"], probe=probe))
    assert sel.ok and sel.relay == "wss://b"


# --- _run_probe: connect/subscribe/publish/read-back ordering ------------

class _FakeEvent:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeRelayHub:
    """A minimal in-memory relay: delivers a published event only to a live
    subscriber (ephemeral semantics — no storage, live subscribers only)."""

    def __init__(self) -> None:
        self._subscribers: List[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def publish(self, content: str) -> None:
        for q in self._subscribers:
            q.put_nowait(_FakeEvent(content))


class _FakeManager:
    def __init__(self, hub: Optional[_FakeRelayHub], *, reachable: bool = True,
                 is_reader: bool = False) -> None:
        self._hub = hub
        self._reachable = reachable
        self._is_reader = is_reader
        self.connected = False
        self.relays = ["r"]
        self.closed = False

    async def connect(self) -> None:
        if not self._reachable:
            self.relays = []           # matches Manager dropping unreachable relays
            self.connected = True
            return
        self.connected = True

    async def get_events(self, *filters, single_event=False, only_stored=False):
        assert self._is_reader and self._hub is not None
        q = self._hub.subscribe()
        while True:
            ev = await q.get()
            yield ev
            if single_event:
                break

    async def close(self) -> None:
        self.closed = True


def test_run_probe_ok_when_event_read_back() -> None:
    hub = _FakeRelayHub()
    reader = _FakeManager(hub, is_reader=True)
    writer = _FakeManager(hub)

    async def publish():
        hub.publish(f"{PROBE_CONTENT_PREFIX}abc123")

    res = asyncio.run(_run_probe(
        "wss://fake", reader, writer, reader_pub="deadbeef", marker="abc123",
        publish=publish, timeout=2.0, settle=0.0))
    assert res.status is ProbeStatus.OK and res.ok
    assert res.rtt_ms is not None
    assert reader.closed and writer.closed  # both connections cleaned up


def test_run_probe_unreachable_reader() -> None:
    reader = _FakeManager(None, reachable=False, is_reader=True)
    writer = _FakeManager(None)

    async def publish():
        return None

    res = asyncio.run(_run_probe(
        "wss://fake", reader, writer, reader_pub="x", marker="m",
        publish=publish, timeout=1.0, settle=0.0))
    assert res.status is ProbeStatus.UNREACHABLE
    assert reader.closed and writer.closed


def test_run_probe_no_readback_times_out() -> None:
    # Live subscriber, but the writer never publishes -> NO_READBACK, not a hang.
    hub = _FakeRelayHub()
    reader = _FakeManager(hub, is_reader=True)
    writer = _FakeManager(hub)

    async def never_publish():
        return None

    res = asyncio.run(_run_probe(
        "wss://fake", reader, writer, reader_pub="x", marker="m",
        publish=never_publish, timeout=0.3, settle=0.0))
    assert res.status is ProbeStatus.NO_READBACK
    assert reader.closed and writer.closed


def test_run_probe_ignores_foreign_event() -> None:
    # A relay that echoes an unrelated event must not pass the probe.
    hub = _FakeRelayHub()
    reader = _FakeManager(hub, is_reader=True)
    writer = _FakeManager(hub)

    async def publish_wrong_marker():
        hub.publish(f"{PROBE_CONTENT_PREFIX}not-our-marker")

    res = asyncio.run(_run_probe(
        "wss://fake", reader, writer, reader_pub="x", marker="the-real-marker",
        publish=publish_wrong_marker, timeout=0.3, settle=0.0))
    assert res.status is ProbeStatus.NO_READBACK
