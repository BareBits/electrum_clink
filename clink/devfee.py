"""Dev-fee accounting: accrual ledger and payout gating.

The dev fee is an opt-out contribution that funds further development of the
plugin. It accrues as a small fraction of every inbound payment answered through
a CLINK offer; once the owed balance crosses a threshold the plugin forwards it
to the BareBits Lightning address.

This module holds the *pure* accounting and policy: how much to accrue, when a
payout is allowed, and how much to send. All I/O (the actual LNURL resolution
and Lightning payment) lives in the runtime — keeping the rules here unit-testable
with a plain dict and an injected clock, mirroring :mod:`clink.offers` and
:mod:`clink.liquidity`.

Money is tracked in **millisats** internally so that small percentages of small
payments don't round to zero and accumulate without loss; payouts are made in
whole sats.
"""

from __future__ import annotations

from typing import Any, Callable, List, MutableMapping, Optional

# Don't bother paying out until at least this much has accrued.
MIN_PAYOUT_SAT = 1_000
# Never forward more than this within any rolling 24h window — a backstop against
# a bug ever draining the wallet, independent of the once-per-day attempt gate.
DAILY_CAP_SAT = 10_000
# At most one payout *attempt* per this interval, counting both successes and
# failures (so a failing payment can't be retried in a tight loop).
ATTEMPT_INTERVAL_SEC = 24 * 60 * 60
# Bound on remembered unpaid invoice hashes, so the pending map can't grow without
# limit if many issued invoices are never paid.
PENDING_MAX = 1_000


class DevFeeLedger:
    """Persisted dev-fee balance plus the rules for when/how much to pay out.

    ``storage`` is any mutable mapping persisted by the host (the plugin passes
    the wallet DB's plugin storage); ``clock_fn`` returns the current unix time;
    ``enabled_fn``/``rate_fn`` read the live config so toggling the fee or moving
    the slider takes effect immediately.
    """

    ACCRUED_KEY = "devfee_accrued_msat"
    LAST_ATTEMPT_KEY = "devfee_last_attempt_ts"
    PAYMENTS_KEY = "devfee_payments"        # [[ts, amount_sat], ...] within ~24h
    PENDING_KEY = "devfee_pending"          # {rhash_hex: issued_ts}

    def __init__(
        self,
        storage: MutableMapping[str, Any],
        *,
        clock_fn: Callable[[], float],
        enabled_fn: Callable[[], bool],
        rate_fn: Callable[[], float],
    ) -> None:
        self._storage = storage
        self._now_fn = clock_fn
        self._enabled_fn = enabled_fn
        self._rate_fn = rate_fn

    # --- persisted scalars ----------------------------------------------

    @property
    def accrued_msat(self) -> int:
        return int(self._storage.get(self.ACCRUED_KEY, 0))

    @accrued_msat.setter
    def accrued_msat(self, value: int) -> None:
        self._storage[self.ACCRUED_KEY] = max(0, int(value))

    @property
    def last_attempt_ts(self) -> int:
        return int(self._storage.get(self.LAST_ATTEMPT_KEY, 0))

    @last_attempt_ts.setter
    def last_attempt_ts(self, value: int) -> None:
        self._storage[self.LAST_ATTEMPT_KEY] = int(value)

    def owed_sat(self) -> int:
        """Whole sats currently owed (the fractional remainder stays accrued)."""
        return self.accrued_msat // 1000

    # --- config snapshots ------------------------------------------------

    def enabled(self) -> bool:
        return bool(self._enabled_fn())

    def rate_percent(self) -> float:
        return float(self._rate_fn())

    # --- accrual ---------------------------------------------------------

    def accrue(self, received_sat: int) -> int:
        """Add this inbound payment's share to the balance. Returns msat added.

        A no-op (returns 0) when the fee is disabled, the amount is non-positive,
        or the rate rounds the contribution below one millisat.
        """
        if not self.enabled() or received_sat <= 0:
            return 0
        rate = self.rate_percent()
        if rate <= 0:
            return 0
        # received_sat * 1000 msat/sat * (rate% / 100)
        fee_msat = int(received_sat * 1000 * rate / 100.0)
        if fee_msat <= 0:
            return 0
        self.accrued_msat = self.accrued_msat + fee_msat
        return fee_msat

    # --- pending (clink-issued, unpaid) invoice hashes -------------------
    #
    # Fees accrue ONLY on invoices CLINK itself issued, so we remember each
    # issued payment hash and accrue when (and only when) that hash is later
    # reported paid. Persisted so a restart between issue and payment is safe.

    def _pending(self) -> dict:
        raw = self._storage.get(self.PENDING_KEY)
        return dict(raw) if isinstance(raw, dict) else {}

    def mark_issued(self, rhash: str) -> None:
        pending = self._pending()
        pending[rhash] = int(self._now_fn())
        # Evict oldest first if we somehow exceed the cap (stale, never-paid).
        if len(pending) > PENDING_MAX:
            for old in sorted(pending, key=pending.get)[: len(pending) - PENDING_MAX]:
                pending.pop(old, None)
        self._storage[self.PENDING_KEY] = pending

    def take_issued(self, rhash: str) -> bool:
        """Consume a remembered issued hash; True iff it was ours."""
        pending = self._pending()
        if rhash in pending:
            del pending[rhash]
            self._storage[self.PENDING_KEY] = pending
            return True
        return False

    # --- payout policy ---------------------------------------------------

    def _payments_window(self) -> List[list]:
        """Recorded payouts within the last 24h, pruned of older entries."""
        now = self._now_fn()
        raw = self._storage.get(self.PAYMENTS_KEY, [])
        window = [p for p in raw if isinstance(p, (list, tuple)) and len(p) == 2
                  and now - p[0] < ATTEMPT_INTERVAL_SEC]
        if window != raw:
            self._storage[self.PAYMENTS_KEY] = window
        return window

    def paid_last_24h_sat(self) -> int:
        return sum(int(p[1]) for p in self._payments_window())

    def cap_remaining_sat(self) -> int:
        return max(0, DAILY_CAP_SAT - self.paid_last_24h_sat())

    def payable_sat(self) -> int:
        """How much we'd send right now: owed, clamped to the 24h cap."""
        return max(0, min(self.owed_sat(), self.cap_remaining_sat()))

    def attempt_due(self) -> bool:
        """True once the once-per-24h attempt gate has elapsed."""
        return self._now_fn() >= self.last_attempt_ts + ATTEMPT_INTERVAL_SEC

    def should_attempt(self, *, ignore_interval: bool = False) -> bool:
        """Whether a payout attempt should be made now.

        ``ignore_interval`` skips only the once-per-day gate (used by a manual
        "pay now"); the threshold and the 24h spend cap always apply.
        """
        if not self.enabled():
            return False
        if self.owed_sat() < MIN_PAYOUT_SAT:
            return False
        if not ignore_interval and not self.attempt_due():
            return False
        return self.payable_sat() >= MIN_PAYOUT_SAT

    # --- record the outcome of an attempt --------------------------------

    def record_success(self, amount_sat: int) -> None:
        """A payout of ``amount_sat`` cleared: debit it and stamp the window."""
        self.accrued_msat = self.accrued_msat - amount_sat * 1000
        window = self._payments_window()
        window.append([int(self._now_fn()), int(amount_sat)])
        self._storage[self.PAYMENTS_KEY] = window
        self.last_attempt_ts = int(self._now_fn())

    def record_failure(self) -> None:
        """A payout attempt failed: stamp the clock so we wait a full day."""
        self.last_attempt_ts = int(self._now_fn())

    # --- introspection (CLI / Qt) ----------------------------------------

    def status(self) -> dict:
        return {
            "enabled": self.enabled(),
            "rate_percent": self.rate_percent(),
            "owed_sat": self.owed_sat(),
            "accrued_msat": self.accrued_msat,
            "paid_last_24h_sat": self.paid_last_24h_sat(),
            "cap_remaining_sat": self.cap_remaining_sat(),
            "last_attempt_ts": self.last_attempt_ts,
            "attempt_due": self.attempt_due(),
            "pending_invoices": len(self._pending()),
        }
