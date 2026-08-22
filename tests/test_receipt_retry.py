"""Unit tests for the runtime side of receipt fast-retries.

A failed receipt publish inside the fast-retry window must schedule a
short-backoff retry tail instead of parking the receipt until the hourly
redelivery tick; a success resets the failure streak. Harness style mirrors
``test_dispatch.py``: a ``ClinkServer`` shell over a real ``ReceiptRegistry``
(hand-cranked clock) with the relay publish faked at the module boundary.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

import clink.clink_plugin as plugin_mod
from clink.clink_plugin import ClinkServer
from clink.receipts import (
    FAIL_RETRY_BACKOFF_SEC,
    FAST_RETRY_WINDOW_SEC,
    RESEND_OFFSETS_SEC,
    ReceiptRegistry,
    ReceiptTarget,
)


class Clock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


ScheduledTail = Tuple[ReceiptTarget, float, int]


def _server(clock: Clock) -> Tuple[Any, List[ScheduledTail]]:
    """A ``ClinkServer`` shell wired with what ``_deliver_receipt`` touches.

    ``_schedule_receipt_rebroadcast`` is replaced with a recorder, so tests
    assert on the (target, delay, fail_streak) scheduling decisions without
    real sleeps.
    """
    from electrum.logging import Logger

    server = ClinkServer.__new__(ClinkServer)
    Logger.__init__(server)
    server.manager = SimpleNamespace()
    server.receipts = ReceiptRegistry({}, clock_fn=clock)
    server._receipt_tails = set()
    server._record = lambda *a, **k: None  # type: ignore[method-assign]
    server._encrypt_event_args = (  # type: ignore[method-assign]
        lambda to, req, payload: {"payload": payload})

    scheduled: List[ScheduledTail] = []

    async def schedule(target: ReceiptTarget, delay: float, *,
                       fail_streak: int = 0) -> None:
        scheduled.append((target, delay, fail_streak))

    server._schedule_receipt_rebroadcast = schedule  # type: ignore[method-assign]
    return server, scheduled


def _owed_target(server: Any) -> ReceiptTarget:
    server.receipts.remember("r", "p", "q",
                             expires_at=server.receipts._now_fn() + 2_000)
    target = server.receipts.mark_due("r")
    assert target is not None
    return target


def _fail_publish(monkeypatch: pytest.MonkeyPatch,
                  calls: Optional[List[int]] = None) -> None:
    async def boom(manager: Any, **kwargs: Any) -> str:
        if calls is not None:
            calls.append(1)
        raise OSError("network flake")
    monkeypatch.setattr(plugin_mod, "add_event_checked", boom)


def _ok_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(manager: Any, **kwargs: Any) -> str:
        return "eventid"
    monkeypatch.setattr(plugin_mod, "add_event_checked", ok)


def test_failed_publish_schedules_backoff_retry(
        monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    server, scheduled = _server(clock)
    target = _owed_target(server)
    _fail_publish(monkeypatch)

    assert asyncio.run(server._deliver_receipt(target)) is False
    assert scheduled == [(target, float(FAIL_RETRY_BACKOFF_SEC[0]), 1)]
    assert server.receipts.is_owed("r")  # still owed, nothing marked sent


def test_consecutive_failures_walk_the_backoff(
        monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    server, scheduled = _server(clock)
    target = _owed_target(server)
    _fail_publish(monkeypatch)

    for streak, expected in enumerate(FAIL_RETRY_BACKOFF_SEC, start=1):
        asyncio.run(server._deliver_receipt(target, fail_streak=streak - 1))
        assert scheduled[-1] == (target, float(expected), streak)


def test_failure_outside_window_falls_back_to_hourly(
        monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    server, scheduled = _server(clock)
    target = _owed_target(server)
    _fail_publish(monkeypatch)
    clock.tick(FAST_RETRY_WINDOW_SEC + 1)

    assert asyncio.run(server._deliver_receipt(target)) is False
    assert scheduled == []  # hourly redelivery loop owns it now
    assert server.receipts.is_owed("r")


def test_success_resets_the_failure_streak(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # A delivery that succeeds after failures schedules the next re-broadcast
    # with a clean streak (0) and the normal schedule delay.
    clock = Clock()
    server, scheduled = _server(clock)
    target = _owed_target(server)
    _ok_publish(monkeypatch)

    assert asyncio.run(server._deliver_receipt(target, fail_streak=3)) is True
    (next_target, delay, streak), = scheduled
    assert next_target.sends == 1
    assert delay == float(RESEND_OFFSETS_SEC[0])
    assert streak == 0


def test_tail_threads_the_streak_through(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # The retry tail passes its streak into the delivery attempt, so the next
    # failure schedules the *next* backoff step, not the first.
    clock = Clock()
    server, scheduled = _server(clock)
    target = _owed_target(server)
    calls: List[int] = []
    _fail_publish(monkeypatch, calls)

    asyncio.run(server._rebroadcast_receipt_later(target, 0.0, 2))
    assert calls == [1]
    assert scheduled == [(target, float(FAIL_RETRY_BACKOFF_SEC[2]), 3)]


def test_tail_skips_delivery_once_no_longer_owed(
        monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    server, scheduled = _server(clock)
    target = _owed_target(server)
    calls: List[int] = []
    _fail_publish(monkeypatch, calls)
    server.receipts.forget("r")

    asyncio.run(server._rebroadcast_receipt_later(target, 0.0, 1))
    assert calls == []
    assert scheduled == []
