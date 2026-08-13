"""Unit tests for the periodic relay liveness monitor.

All network I/O is behind the injected ``probe`` coroutine, and time behind the
injected ``clock_fn``/``sleep`` — so these tests cover the retry-once policy,
result bookkeeping, and pruning with no relays and no waiting.

Following the repo convention (no pytest-asyncio), each test drives its
coroutine with ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, List, Optional

from clink.liveness import RETRY_DELAY_SEC, LivenessResult, RelayLivenessMonitor
from clink.relay_probe import ProbeResult, ProbeStatus


class FakeProbe:
    """Scripted probe: pops the next status per relay; records every call."""

    def __init__(self, script: Dict[str, List[ProbeStatus]]) -> None:
        self.script = {relay: list(statuses) for relay, statuses in script.items()}
        self.calls: List[str] = []

    async def __call__(self, relay: str) -> ProbeResult:
        self.calls.append(relay)
        statuses = self.script.get(relay)
        status = statuses.pop(0) if statuses else ProbeStatus.NO_READBACK
        rtt = 12 if status is ProbeStatus.OK else None
        return ProbeResult(relay, status, rtt_ms=rtt, detail="scripted")


def _monitor(probe: Callable[[str], Awaitable[ProbeResult]], *,
             sleeps: Optional[List[float]] = None,
             retry_delay_sec: float = RETRY_DELAY_SEC) -> RelayLivenessMonitor:
    async def fake_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return RelayLivenessMonitor(
        probe, clock_fn=lambda: 1000.0, sleep=fake_sleep,
        retry_delay_sec=retry_delay_sec)


# --- retry-once policy ----------------------------------------------------

def test_ok_first_try_probes_once_and_never_sleeps() -> None:
    probe = FakeProbe({"wss://a": [ProbeStatus.OK]})
    sleeps: List[float] = []
    monitor = _monitor(probe, sleeps=sleeps)

    results = asyncio.run(monitor.run_once(["wss://a"]))

    res = results["wss://a"]
    assert res.ok and res.status is ProbeStatus.OK
    assert res.retried is False
    assert res.rtt_ms == 12
    assert res.checked_at == 1000.0
    assert probe.calls == ["wss://a"]
    assert sleeps == []


def test_transient_failure_recovers_on_retry() -> None:
    probe = FakeProbe({"wss://a": [ProbeStatus.NO_READBACK, ProbeStatus.OK]})
    sleeps: List[float] = []
    monitor = _monitor(probe, sleeps=sleeps, retry_delay_sec=7.0)

    results = asyncio.run(monitor.run_once(["wss://a"]))

    res = results["wss://a"]
    assert res.ok is True          # the blip never surfaces as "down"
    assert res.retried is True     # ...but the retry is recorded
    assert probe.calls == ["wss://a", "wss://a"]
    assert sleeps == [7.0]         # waited the configured delay before retrying
    assert monitor.down_relays() == []


def test_double_failure_is_surfaced_with_second_verdict() -> None:
    probe = FakeProbe({"wss://a": [ProbeStatus.NO_READBACK, ProbeStatus.UNREACHABLE]})
    monitor = _monitor(probe)

    results = asyncio.run(monitor.run_once(["wss://a"]))

    res = results["wss://a"]
    assert res.ok is False and res.retried is True
    assert res.status is ProbeStatus.UNREACHABLE  # the confirming probe's verdict
    assert [r.relay for r in monitor.down_relays()] == ["wss://a"]


def test_run_once_retry_delay_overrides_default() -> None:
    probe = FakeProbe({"wss://a": [ProbeStatus.NO_READBACK, ProbeStatus.OK]})
    sleeps: List[float] = []
    monitor = _monitor(probe, sleeps=sleeps, retry_delay_sec=60.0)

    asyncio.run(monitor.run_once(["wss://a"], retry_delay=1.0))

    assert sleeps == [1.0]


# --- input hygiene + bookkeeping -------------------------------------------

def test_relays_are_deduped_and_blanks_dropped() -> None:
    probe = FakeProbe({"wss://a": [ProbeStatus.OK], "wss://b": [ProbeStatus.OK]})
    monitor = _monitor(probe)

    results = asyncio.run(monitor.run_once(
        ["wss://a", "", "  ", "wss://a", " wss://b "]))

    assert sorted(results) == ["wss://a", "wss://b"]
    assert sorted(probe.calls) == ["wss://a", "wss://b"]  # one probe each


def test_no_offers_means_no_probes() -> None:
    probe = FakeProbe({})
    monitor = _monitor(probe)

    results = asyncio.run(monitor.run_once([]))

    assert results == {} and probe.calls == []


def test_removed_relays_are_pruned_from_results() -> None:
    probe = FakeProbe({
        "wss://a": [ProbeStatus.NO_READBACK, ProbeStatus.NO_READBACK],
        "wss://b": [ProbeStatus.OK, ProbeStatus.OK],
    })
    monitor = _monitor(probe)

    asyncio.run(monitor.run_once(["wss://a", "wss://b"]))
    assert sorted(monitor.results) == ["wss://a", "wss://b"]

    # The offer advertising wss://a was removed: its stale verdict must go too.
    results = asyncio.run(monitor.run_once(["wss://b"]))
    assert list(results) == ["wss://b"]
    assert list(monitor.results) == ["wss://b"]
    assert monitor.down_relays() == []


def test_to_dict_shape_matches_cli_and_qt_consumers() -> None:
    res = LivenessResult(
        relay="wss://a", ok=False, status=ProbeStatus.UNREACHABLE,
        detail="connect timed out", rtt_ms=None, checked_at=1000.0, retried=True)
    assert res.to_dict() == {
        "relay": "wss://a",
        "ok": False,
        "status": "unreachable",
        "detail": "connect timed out",
        "rtt_ms": None,
        "checked_at": 1000.0,
        "retried": True,
    }


def test_relays_are_probed_concurrently() -> None:
    """A slow relay must not serialise the sweep: with N relays each taking one
    'tick', a concurrent run finishes in ~1 tick, a serial one in N."""
    started: List[str] = []
    release = asyncio.Event()

    async def probe(relay: str) -> ProbeResult:
        started.append(relay)
        await release.wait()
        return ProbeResult(relay, ProbeStatus.OK)

    async def scenario() -> Dict[str, LivenessResult]:
        monitor = RelayLivenessMonitor(probe, clock_fn=lambda: 0.0)
        task = asyncio.ensure_future(monitor.run_once(["wss://a", "wss://b"]))
        for _ in range(10):  # a few ticks: run_once -> gather -> probes start
            await asyncio.sleep(0)
            if len(started) == 2:
                break
        assert sorted(started) == ["wss://a", "wss://b"]  # both in flight at once
        release.set()
        return await task

    results = asyncio.run(scenario())
    assert all(r.ok for r in results.values())
