"""Unit tests for the liquidity reserver (pure, with a fake clock)."""

from __future__ import annotations

from clink.liquidity import LiquidityReserver


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_reserve_reduces_available() -> None:
    clock = FakeClock()
    r = LiquidityReserver(capacity_fn=lambda: 100_000, clock_fn=clock)
    assert r.available_sat() == 100_000
    assert r.try_reserve("a", 40_000, expires_at=clock.t + 120)
    assert r.available_sat() == 60_000
    assert r.reserved_sat() == 40_000


def test_second_request_cannot_use_locked_liquidity() -> None:
    clock = FakeClock()
    r = LiquidityReserver(capacity_fn=lambda: 50_000, clock_fn=clock)
    assert r.try_reserve("a", 40_000, expires_at=clock.t + 120)
    # only 10k left; a 20k request must be refused
    assert not r.try_reserve("b", 20_000, expires_at=clock.t + 120)
    assert r.try_reserve("c", 10_000, expires_at=clock.t + 120)


def test_expiry_frees_liquidity() -> None:
    clock = FakeClock()
    r = LiquidityReserver(capacity_fn=lambda: 50_000, clock_fn=clock)
    assert r.try_reserve("a", 50_000, expires_at=clock.t + 120)
    assert r.available_sat() == 0
    clock.t += 121  # invoice expired
    assert r.available_sat() == 50_000
    assert r.reserved_sat() == 0


def test_release_frees_liquidity_early() -> None:
    clock = FakeClock()
    r = LiquidityReserver(capacity_fn=lambda: 50_000, clock_fn=clock)
    r.try_reserve("a", 30_000, expires_at=clock.t + 120)
    r.release("a")
    assert r.available_sat() == 50_000


def test_reject_nonpositive_amount() -> None:
    r = LiquidityReserver(capacity_fn=lambda: 50_000, clock_fn=FakeClock())
    assert not r.try_reserve("a", 0, expires_at=9_999_999)
    assert not r.try_reserve("a", -5, expires_at=9_999_999)


def test_capacity_drop_never_goes_negative() -> None:
    # capacity can shrink under us (channel state changed); stay clamped at 0
    cap = {"v": 50_000}
    clock = FakeClock()
    r = LiquidityReserver(capacity_fn=lambda: cap["v"], clock_fn=clock)
    r.try_reserve("a", 40_000, expires_at=clock.t + 120)
    cap["v"] = 10_000
    assert r.available_sat() == 0
