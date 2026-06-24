"""Unit tests for the dev-fee ledger (pure, with a fake clock and dict storage)."""

from __future__ import annotations

from typing import Any, Dict

from clink.devfee import (
    ATTEMPT_INTERVAL_SEC,
    DAILY_CAP_SAT,
    MIN_PAYOUT_SAT,
    DevFeeLedger,
)


class FakeClock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_ledger(*, enabled: bool = True, rate: float = 0.1):
    storage: Dict[str, Any] = {}
    cfg = {"enabled": enabled, "rate": rate}
    clock = FakeClock()
    ledger = DevFeeLedger(
        storage,
        clock_fn=clock,
        enabled_fn=lambda: cfg["enabled"],
        rate_fn=lambda: cfg["rate"],
    )
    return ledger, storage, cfg, clock


# --- accrual ---------------------------------------------------------------

def test_accrue_basic_rate() -> None:
    ledger, _, _, _ = make_ledger(rate=0.1)
    # 0.1% of 1,000,000 sat = 1,000 sat = 1,000,000 msat
    added = ledger.accrue(1_000_000)
    assert added == 1_000_000
    assert ledger.owed_sat() == 1_000


def test_accrue_millisat_precision_no_rounding_loss() -> None:
    # Each 1-sat payment at 0.1% accrues 1 msat; integer-sat rounding would lose
    # every one of them. The msat ledger keeps them and they sum to a real sat.
    ledger, _, _, _ = make_ledger(rate=0.1)
    for _ in range(1000):
        assert ledger.accrue(1) == 1
    assert ledger.accrued_msat == 1000
    assert ledger.owed_sat() == 1


def test_accrue_disabled_is_noop() -> None:
    ledger, _, _, _ = make_ledger(enabled=False)
    assert ledger.accrue(1_000_000) == 0
    assert ledger.owed_sat() == 0


def test_accrue_zero_rate_is_noop() -> None:
    ledger, _, _, _ = make_ledger(rate=0.0)
    assert ledger.accrue(1_000_000) == 0


def test_accrue_nonpositive_amount_is_noop() -> None:
    ledger, _, _, _ = make_ledger()
    assert ledger.accrue(0) == 0
    assert ledger.accrue(-5) == 0


def test_owed_sat_floors_fractional_remainder() -> None:
    ledger, _, _, _ = make_ledger(rate=0.1)
    ledger.accrue(1_500)  # 0.1% of 1500 = 1.5 sat = 1500 msat
    assert ledger.accrued_msat == 1_500
    # owed floors to whole sats; the extra 500 msat stays accrued for next time
    assert ledger.owed_sat() == 1


def test_rate_change_takes_effect_immediately() -> None:
    ledger, _, cfg, _ = make_ledger(rate=0.1)
    ledger.accrue(100_000)            # 100 sat
    cfg["rate"] = 5.0
    ledger.accrue(100_000)            # 5000 sat
    assert ledger.owed_sat() == 5_100


# --- pending (clink-issued) hashes ----------------------------------------

def test_mark_and_take_issued() -> None:
    ledger, _, _, _ = make_ledger()
    ledger.mark_issued("abc")
    assert ledger.take_issued("abc") is True
    # second take is a miss (already consumed)
    assert ledger.take_issued("abc") is False
    # unknown hash is a miss
    assert ledger.take_issued("zzz") is False


def test_pending_is_persisted() -> None:
    ledger, storage, _, _ = make_ledger()
    ledger.mark_issued("abc")
    assert "abc" in storage[DevFeeLedger.PENDING_KEY]


# --- payout gating ---------------------------------------------------------

def test_no_attempt_below_threshold() -> None:
    ledger, _, _, _ = make_ledger(rate=0.1)
    ledger.accrue(999_000)  # 999 sat owed
    assert ledger.owed_sat() == 999
    assert ledger.should_attempt() is False


def test_attempt_due_at_threshold() -> None:
    ledger, _, _, _ = make_ledger(rate=0.1)
    ledger.accrue(1_000_000)  # exactly 1000 sat
    assert ledger.should_attempt() is True


def test_disabled_blocks_attempt() -> None:
    ledger, _, cfg, _ = make_ledger(rate=0.1)
    ledger.accrue(2_000_000)
    cfg["enabled"] = False
    assert ledger.should_attempt() is False


def test_once_per_24h_gate() -> None:
    ledger, _, _, clock = make_ledger(rate=5.0)
    ledger.accrue(1_000_000)  # 50_000 sat owed (capped on payout)
    assert ledger.should_attempt() is True
    ledger.record_failure()                 # stamps the clock, any outcome
    assert ledger.should_attempt() is False  # within 24h: blocked
    clock.t += ATTEMPT_INTERVAL_SEC - 1
    assert ledger.should_attempt() is False
    clock.t += 2                             # past 24h
    assert ledger.should_attempt() is True


def test_ignore_interval_forces_attempt_despite_gate() -> None:
    ledger, _, _, _ = make_ledger(rate=5.0)
    ledger.accrue(1_000_000)
    ledger.record_failure()
    assert ledger.should_attempt() is False
    assert ledger.should_attempt(ignore_interval=True) is True


# --- recording outcomes ----------------------------------------------------

def test_record_success_debits_and_logs_payment() -> None:
    ledger, _, _, clock = make_ledger(rate=5.0)
    ledger.accrue(100_000)  # 5000 sat owed
    ledger.record_success(5_000)
    assert ledger.owed_sat() == 0
    assert ledger.paid_last_24h_sat() == 5_000
    assert ledger.last_attempt_ts == int(clock.t)


def test_record_failure_keeps_balance() -> None:
    ledger, _, _, clock = make_ledger(rate=5.0)
    ledger.accrue(100_000)
    ledger.record_failure()
    assert ledger.owed_sat() == 5_000          # nothing debited
    assert ledger.last_attempt_ts == int(clock.t)


# --- 10k / 24h spend cap ---------------------------------------------------

def test_payable_clamped_to_daily_cap() -> None:
    ledger, _, _, _ = make_ledger(rate=5.0)
    ledger.accrue(300_000)  # 15_000 sat owed, above the 10_000 cap
    assert ledger.owed_sat() == 15_000
    assert ledger.payable_sat() == DAILY_CAP_SAT


def test_daily_cap_exhausts_then_resets_after_24h() -> None:
    ledger, _, _, clock = make_ledger(rate=5.0)
    ledger.accrue(300_000)  # 15_000 sat owed
    ledger.record_success(DAILY_CAP_SAT)        # spend the whole daily cap
    assert ledger.owed_sat() == 5_000
    assert ledger.cap_remaining_sat() == 0
    # Even ignoring the once-a-day gate, the cap blocks a further payout now.
    assert ledger.payable_sat() < MIN_PAYOUT_SAT
    assert ledger.should_attempt(ignore_interval=True) is False
    # After 24h the recorded payment ages out of the window and the cap resets.
    clock.t += ATTEMPT_INTERVAL_SEC + 1
    assert ledger.cap_remaining_sat() == DAILY_CAP_SAT
    assert ledger.payable_sat() == 5_000
    assert ledger.should_attempt() is True


def test_status_snapshot_shape() -> None:
    ledger, _, _, _ = make_ledger(rate=0.1)
    ledger.accrue(1_000_000)
    status = ledger.status()
    assert status["enabled"] is True
    assert status["rate_percent"] == 0.1
    assert status["owed_sat"] == 1_000
    assert "cap_remaining_sat" in status and "attempt_due" in status
