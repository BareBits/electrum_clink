"""Inbound-liquidity reservation.

When we answer a noffer request with a BOLT-11 invoice, that invoice "locks up"
the inbound liquidity it needs until it is either paid or expires. Without this,
two concurrent requests could each be told there is room, then race for the same
inbound capacity and one payment would fail.

This component is deliberately pure (no Electrum imports): it is given a
``capacity_fn`` returning the *current* real receivable capacity in sats and a
``clock_fn`` returning monotonic-ish seconds, so it is fully unit-testable. The
plugin wires ``capacity_fn`` to ``lnworker.num_sats_can_receive()`` and releases
a reservation early when its invoice settles (capacity then drops for real).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


def receivable_capacity_sat(lnworker: Optional[Any]) -> int:
    """Current inbound (receivable) capacity in sats, clamped to >= 0.

    ``lnworker`` is ``None`` whenever the wallet has no Lightning or is mid
    shutdown — ``Wallet.stop()`` clears it while UI pollers may still be
    running — so that case must be treated as zero capacity rather than
    dereferenced. Kept here (free of Electrum imports) so it stays unit-testable.
    """
    if lnworker is None:
        return 0
    return max(0, int(lnworker.num_sats_can_receive()))


@dataclass
class Reservation:
    key: str           # payment hash of the issued invoice
    amount_sat: int
    expires_at: float  # epoch seconds; matches the invoice's own expiry


class LiquidityReserver:
    """Tracks soft reservations against live receivable capacity."""

    def __init__(
        self,
        capacity_fn: Callable[[], int],
        clock_fn: Callable[[], float] = time.time,
    ) -> None:
        self._capacity_fn = capacity_fn
        self._clock_fn = clock_fn
        self._reservations: Dict[str, Reservation] = {}
        self._lock = threading.RLock()

    def _sweep(self) -> None:
        now = self._clock_fn()
        expired = [k for k, r in self._reservations.items() if r.expires_at <= now]
        for k in expired:
            del self._reservations[k]

    def reserved_sat(self) -> int:
        with self._lock:
            self._sweep()
            return sum(r.amount_sat for r in self._reservations.values())

    def available_sat(self) -> int:
        """Receivable capacity minus everything currently reserved (never < 0)."""
        with self._lock:
            self._sweep()
            reserved = sum(r.amount_sat for r in self._reservations.values())
            return max(0, int(self._capacity_fn()) - reserved)

    def try_reserve(self, key: str, amount_sat: int, expires_at: float) -> bool:
        """Reserve ``amount_sat`` for ``key`` if it fits; return success."""
        with self._lock:
            self._sweep()
            if amount_sat <= 0:
                return False
            if amount_sat > self.available_sat():
                return False
            self._reservations[key] = Reservation(key, amount_sat, expires_at)
            return True

    def release(self, key: str) -> None:
        """Drop a reservation early (e.g. once its invoice is paid)."""
        with self._lock:
            self._reservations.pop(key, None)

    def active(self) -> List[Reservation]:
        with self._lock:
            self._sweep()
            return list(self._reservations.values())
