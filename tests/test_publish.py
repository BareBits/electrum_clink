"""Unit tests for :mod:`clink.publish` — OK-verdict-aware publishing.

Fake relays modelled on ``electrum_aionostr.Relay``'s public surface
(``event_adds`` + ``send``): the fake's ``send`` immediately resolves the
pending future with the relay's scripted ``OK`` message, exactly like the real
``_receive_messages`` loop does when the relay acknowledges.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from electrum_aionostr.event import Event
from electrum_aionostr.key import PrivateKey

from clink.publish import PublishRejected, add_event_checked

SK = PrivateKey().hex()


class FakeRelay:
    """Scripted relay: answers every EVENT with a fixed OK verdict."""

    def __init__(self, *, accepted: Optional[bool], reason: str = "",
                 send_error: Optional[Exception] = None) -> None:
        # accepted=None -> never acknowledge (no OK message at all)
        self._accepted = accepted
        self._reason = reason
        self._send_error = send_error
        self.event_adds: Dict[str, "asyncio.Future[list]"] = {}
        self.sent: List[Any] = []

    async def send(self, message: List[Any]) -> None:
        if self._send_error is not None:
            raise self._send_error
        self.sent.append(message)
        event_id = message[1]["id"]
        if self._accepted is None:
            return  # OK-less relay: leave the future pending
        fut = self.event_adds[event_id]
        if not fut.done():
            fut.set_result(["OK", event_id, self._accepted, self._reason])


def _manager(*relays: FakeRelay) -> SimpleNamespace:
    return SimpleNamespace(relays=list(relays))


def _publish(manager: SimpleNamespace, *, timeout: float = 0.05) -> str:
    return asyncio.run(add_event_checked(
        manager, kind=21001, tags=[["p", "ab" * 32]], content="hello",
        private_key=SK, timeout=timeout))


def test_accepted_publish_returns_signed_event_id() -> None:
    relay = FakeRelay(accepted=True)
    event_id = _publish(_manager(relay))
    # The event that went over the wire is a properly signed kind-21001 event
    # whose id matches the returned one.
    [sent] = relay.sent
    assert sent[0] == "EVENT"
    event = Event.from_json(sent[1])
    assert event.id == event_id
    assert event.kind == 21001
    assert event.content == "hello"
    assert ["p", "ab" * 32] in event.tags
    assert event.verify()


def test_all_relays_reject_raises_with_reason() -> None:
    relay = FakeRelay(accepted=False, reason="blocked: kind 21001 not allowed")
    with pytest.raises(PublishRejected, match="kind 21001 not allowed"):
        _publish(_manager(relay))


def test_one_acceptance_wins_over_rejections() -> None:
    ok = FakeRelay(accepted=True)
    nope = FakeRelay(accepted=False, reason="blocked")
    event_id = _publish(_manager(nope, ok))
    assert event_id
    # the pending futures are cleaned up either way
    assert not ok.event_adds and not nope.event_adds


def test_no_acknowledgment_times_out_like_stock_add_event() -> None:
    relay = FakeRelay(accepted=None)
    with pytest.raises(asyncio.TimeoutError):
        _publish(_manager(relay))
    assert not relay.event_adds  # no leaked future


def test_no_relays_times_out_immediately() -> None:
    with pytest.raises(asyncio.TimeoutError):
        _publish(_manager())


def test_send_failure_is_not_a_rejection() -> None:
    # A relay whose send blows up (connection trouble) must not masquerade as
    # an explicit rejection; with no other relay it is a timeout-style failure.
    relay = FakeRelay(accepted=True, send_error=ConnectionError("gone"))
    with pytest.raises(asyncio.TimeoutError):
        _publish(_manager(relay))


def test_send_failure_plus_rejection_reports_the_rejection() -> None:
    broken = FakeRelay(accepted=True, send_error=ConnectionError("gone"))
    nope = FakeRelay(accepted=False, reason="rate limited")
    with pytest.raises(PublishRejected, match="rate limited"):
        _publish(_manager(broken, nope))


def test_unknown_relay_internals_fall_back_to_stock_publish(monkeypatch) -> None:
    # A relay object without event_adds/send (future electrum_aionostr
    # refactor) must route the publish through aionostr._add_event untouched.
    calls: List[Dict[str, Any]] = []

    async def fake_add_event(manager: Any, **kwargs: Any) -> str:
        calls.append(kwargs)
        return "stock-id"

    import clink.publish as publish_mod
    monkeypatch.setattr(publish_mod.aionostr, "_add_event", fake_add_event)
    manager = _manager(SimpleNamespace())  # no event_adds / send
    assert _publish(manager) == "stock-id"
    assert calls and calls[0]["kind"] == 21001
