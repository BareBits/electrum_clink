"""Periodic liveness monitor for the relays existing noffers advertise.

A distributed noffer embeds exactly one relay; if that relay later dies or
starts refusing kind-21001 events, the offer silently stops being payable — the
plugin keeps listening, but requests never arrive. Offer creation probes the
relay once, and the manual "Check noffers" self-test exercises it on demand,
but nothing watched it *afterwards*. This module fills that gap.

:class:`RelayLivenessMonitor` re-runs the same payability probe used at offer
creation (:func:`clink.relay_probe.probe_relay_payable`) against every relay an
existing offer's noffer advertises. The runtime drives it from the hourly
receipt-redelivery tick, so it also fires once on every reconnect. A failed
probe is retried once (after :data:`RETRY_DELAY_SEC`) before the relay is
flagged, so a transient blip never raises an alarm.

Only relays advertised by *existing offers* are probed — with no offers there
is nothing a payer could hold, so nothing is checked. Results are session-only
state surfaced in the Qt tab and the ``clink_check_relays`` command; the
monitor never mutates the relay selection (the user decides what to do).

Like the sibling modules, this is pure orchestration over injected
``probe``/``clock``/``sleep`` callables, so it is unit-testable with no network.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from .relay_probe import ProbeResult, ProbeStatus

# Wait this long after a failed probe before the single confirming re-probe.
RETRY_DELAY_SEC: float = 60.0


@dataclass
class LivenessResult:
    """Latest liveness verdict for one advertised relay."""

    relay: str
    ok: bool
    status: ProbeStatus
    detail: str = ""
    rtt_ms: Optional[int] = None
    checked_at: float = 0.0
    # True when the verdict came from the confirming re-probe (i.e. the first
    # probe failed). An ok+retried result means "blipped but recovered".
    retried: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "relay": self.relay,
            "ok": self.ok,
            "status": self.status.value,
            "detail": self.detail,
            "rtt_ms": self.rtt_ms,
            "checked_at": self.checked_at,
            "retried": self.retried,
        }


def _distinct(relays: Iterable[str]) -> List[str]:
    """Strip, drop blanks, dedupe preserving first-seen order."""
    out: List[str] = []
    for url in relays:
        url = (url or "").strip()
        if url and url not in out:
            out.append(url)
    return out


class RelayLivenessMonitor:
    """Tracks payability of the relays advertised by existing noffers.

    ``probe`` runs one payability round-trip against a relay URL; ``clock_fn``
    returns the current unix time; ``sleep`` awaits between a failure and its
    confirming re-probe. All three are injectable for tests.
    """

    def __init__(
        self,
        probe: Callable[[str], Awaitable[ProbeResult]],
        *,
        clock_fn: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_delay_sec: float = RETRY_DELAY_SEC,
    ) -> None:
        self._probe = probe
        self._clock = clock_fn
        self._sleep = sleep
        self._retry_delay = retry_delay_sec
        # Latest result per relay URL — session-only by design.
        self.results: Dict[str, LivenessResult] = {}

    def down_relays(self) -> List[LivenessResult]:
        """Every tracked relay whose latest verdict is not ok."""
        return [r for r in self.results.values() if not r.ok]

    async def _check_one(self, relay: str,
                         retry_delay: Optional[float]) -> LivenessResult:
        delay = self._retry_delay if retry_delay is None else retry_delay
        first = await self._probe(relay)
        retried = False
        result = first
        if not first.ok:
            # Retry once before flagging, so a transient relay blip (restart,
            # brief network hiccup) never surfaces as a dead relay.
            await self._sleep(delay)
            result = await self._probe(relay)
            retried = True
        return LivenessResult(
            relay=relay,
            ok=result.ok,
            status=result.status,
            detail=result.detail,
            rtt_ms=result.rtt_ms,
            checked_at=self._clock(),
            retried=retried,
        )

    async def run_once(self, relays: Iterable[str], *,
                       retry_delay: Optional[float] = None) -> Dict[str, LivenessResult]:
        """Probe every distinct relay in ``relays``; update and return results.

        Relays are probed concurrently; a failing relay is re-probed once after
        the retry delay and the second verdict wins. Results for relays no
        longer in ``relays`` are pruned (their offers were removed).
        """
        urls = _distinct(relays)
        checked = list(await asyncio.gather(
            *(self._check_one(u, retry_delay) for u in urls)))
        self.results = {res.relay: res for res in checked}
        return dict(self.results)
