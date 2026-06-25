"""Unit tests for the persisted receipt registry (pure, dict-backed storage)."""

from __future__ import annotations

from typing import Any, Dict, List

from clink.receipts import (
    MAX_PENDING,
    RETRY_INTERVAL_SEC,
    RETRY_MAX_SEC,
    ReceiptRegistry,
    ReceiptTarget,
)


class Clock:
    """A hand-cranked clock so retry/expiry timing is deterministic."""

    def __init__(self, t: float = 1_000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


def _reg(storage: Dict[str, Any], clock: Clock) -> ReceiptRegistry:
    return ReceiptRegistry(storage, clock_fn=clock)


def test_remember_then_pay_yields_target() -> None:
    storage: Dict[str, Any] = {}
    clock = Clock()
    reg = _reg(storage, clock)
    reg.remember("rhash1", "payerpub", "reqid1", expires_at=clock() + 120)
    assert reg.pending_count() == 1
    assert reg.owed_count() == 0  # not paid yet

    target = reg.mark_due("rhash1")
    assert isinstance(target, ReceiptTarget)
    assert target.payer_pubkey == "payerpub"
    assert target.request_event_id == "reqid1"
    assert reg.owed_count() == 1


def test_mark_due_unknown_hash_returns_none() -> None:
    reg = _reg({}, Clock())
    assert reg.mark_due("never-issued") is None


def test_mark_sent_removes_entry() -> None:
    storage: Dict[str, Any] = {}
    reg = _reg(storage, Clock())
    reg.remember("r", "p", "q", expires_at=2_000)
    reg.mark_due("r")
    reg.mark_sent("r")
    assert reg.pending_count() == 0
    assert reg.due_targets() == []


def test_due_persists_across_reload() -> None:
    # A receipt owed before a restart must still be owed after one.
    storage: Dict[str, Any] = {}
    clock = Clock()
    _reg(storage, clock).remember("r", "payer", "req", expires_at=clock() + 120)
    _reg(storage, clock).mark_due("r")

    reloaded = _reg(storage, clock)
    targets = reloaded.due_targets()
    assert [t.rhash for t in targets] == ["r"]
    assert targets[0].payer_pubkey == "payer"


def test_mark_due_is_idempotent_on_due_since() -> None:
    storage: Dict[str, Any] = {}
    clock = Clock()
    reg = _reg(storage, clock)
    reg.remember("r", "p", "q", expires_at=clock() + 120)
    reg.mark_due("r")
    first_due_since = storage[ReceiptRegistry.STORAGE_KEY]["r"]["due_since"]
    clock.tick(50)
    reg.mark_due("r")  # second settlement signal shouldn't reset the clock
    assert storage[ReceiptRegistry.STORAGE_KEY]["r"]["due_since"] == first_due_since


def test_unpaid_invoice_swept_after_expiry() -> None:
    storage: Dict[str, Any] = {}
    clock = Clock()
    reg = _reg(storage, clock)
    reg.remember("r", "p", "q", expires_at=clock() + 120)
    clock.tick(121)
    reg.sweep()
    assert reg.pending_count() == 0  # never paid, invoice expired -> no receipt owed


def test_paid_receipt_survives_expiry_then_abandoned_after_max() -> None:
    storage: Dict[str, Any] = {}
    clock = Clock()
    reg = _reg(storage, clock)
    reg.remember("r", "p", "q", expires_at=clock() + 120)
    reg.mark_due("r")           # paid right away
    clock.tick(121)
    reg.sweep()
    assert reg.owed_count() == 1  # invoice-expiry doesn't drop an owed receipt

    clock.tick(RETRY_MAX_SEC)
    reg.sweep()
    assert reg.pending_count() == 0  # finally abandoned 10 days after payment


def test_due_targets_throttled_by_retry_interval() -> None:
    storage: Dict[str, Any] = {}
    clock = Clock()
    reg = _reg(storage, clock)
    reg.remember("r", "p", "q", expires_at=clock() + 120)
    reg.mark_due("r")

    # Never attempted -> immediately eligible.
    assert [t.rhash for t in reg.due_targets()] == ["r"]

    reg.record_attempt("r")
    # Within the interval -> not retried yet.
    clock.tick(RETRY_INTERVAL_SEC - 1)
    assert reg.due_targets() == []
    # Past the interval -> eligible again, with the attempt count carried.
    clock.tick(2)
    targets = reg.due_targets()
    assert [t.rhash for t in targets] == ["r"]
    assert targets[0].attempts == 1


def test_due_targets_excludes_awaiting_payment() -> None:
    reg = _reg({}, Clock())
    reg.remember("r", "p", "q", expires_at=10_000)  # issued, not paid
    assert reg.due_targets() == []


def test_forget_drops_entry() -> None:
    storage: Dict[str, Any] = {}
    reg = _reg(storage, Clock())
    reg.remember("r", "p", "q", expires_at=2_000)
    reg.forget("r")
    assert reg.pending_count() == 0


def test_cap_evicts_unpaid_but_never_owed() -> None:
    storage: Dict[str, Any] = {}
    clock = Clock()
    reg = _reg(storage, clock)

    # One owed receipt that must never be evicted.
    reg.remember("owed", "p", "q", expires_at=clock() + 10)
    reg.mark_due("owed")

    # Flood with awaiting-payment entries past the cap; oldest-expiry pruned.
    for i in range(MAX_PENDING + 50):
        reg.remember(f"await{i}", "p", "q", expires_at=clock() + 1_000 + i)

    assert reg.pending_count() <= MAX_PENDING
    assert reg.mark_due("owed") is not None  # the owed receipt is still there
