"""Unit tests for the request-handling hardening in ``ClinkServer``.

Covers the defenses added by the relay/payer security review, with real NIP-44
crypto over fake collaborators (no network):

  * freshness clamps on ``created_at`` — stale, future-dated, and the
    regression where an ``expiration`` tag used to bypass the age check
  * the replay guard — duplicate events dropped, junk (undecryptable) events
    unable to pollute the bounded seen-cache
  * decrypted-request schema validation — mistyped/oversized ``offer`` fields
    answered with a clean protocol error instead of raising
  * the per-payer cap on outstanding unpaid invoices
  * error hygiene — internal exception text never reaches the payer
  * wallet-request garbage collection (``_delete_stale_request``)
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict, deque
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from electrum.invoices import PR_EXPIRED, PR_PAID, PR_UNPAID
from electrum_aionostr.event import Event
from electrum_aionostr.key import PrivateKey

from clink import nip44, protocol
from clink.clink_plugin import (
    CLINK_EVENT_KIND,
    MAX_CLOCK_SKEW_SEC,
    MAX_PENDING_PER_PAYER,
    MAX_REQUEST_AGE_SEC,
    ClinkServer,
)
from clink.offers import Offer


# --- harness -----------------------------------------------------------------

def _dispatch_server() -> Tuple[Any, Dict[str, List[Any]]]:
    """A ``ClinkServer`` shell wired with exactly what ``_dispatch`` touches.

    Real identity keys (so NIP-44 decryption is exercised for real); fake
    offers/reserver/receipts; ``_issue_invoice`` and ``send_response`` replaced
    with recorders.
    """
    from electrum.logging import Logger

    server = ClinkServer.__new__(ClinkServer)
    Logger.__init__(server)
    server_sk = PrivateKey()
    server.private_key = server_sk
    server.pubkey_hex = server_sk.public_key.hex()
    server._seen_events = OrderedDict()
    server._pending_pings = {}
    server.recent_activity = deque(maxlen=50)
    server._selftest_payers = {}
    offer = Offer(offer_id="o1", label="L")
    server.offers = SimpleNamespace(get=lambda oid: offer if oid == "o1" else None)
    server.reserver = SimpleNamespace(available_sat=lambda: 100_000)
    server.receipts = SimpleNamespace(pending_count_for=lambda pub: 0)

    calls: Dict[str, List[Any]] = {"responses": [], "issued": []}

    async def send_response(event: Any, payload: Dict[str, Any]) -> None:
        calls["responses"].append(payload)

    async def issue_invoice(event: Any, offer: Any, amount_sat: int,
                            description: Optional[str] = None, *,
                            selftest: bool = False) -> None:
        calls["issued"].append((amount_sat, description, selftest))

    server.send_response = send_response  # type: ignore[method-assign]
    server._issue_invoice = issue_invoice  # type: ignore[method-assign]
    return server, calls


def _request_event(server: Any, payer_sk: PrivateKey, req: Dict[str, Any], *,
                   created_at: Optional[int] = None,
                   extra_tags: Optional[List[List[str]]] = None) -> Event:
    """A validly encrypted kind-21001 request event addressed to ``server``."""
    content = nip44.encrypt_to(payer_sk.raw_secret, server.pubkey_hex, json.dumps(req))
    return Event(
        pubkey=payer_sk.public_key.hex(),
        kind=CLINK_EVENT_KIND,
        created_at=int(created_at if created_at is not None else time.time()),
        tags=[["p", server.pubkey_hex], *(extra_tags or [])],
        content=content,
    )


def _dispatch(server: Any, event: Event) -> None:
    asyncio.run(server._dispatch(event))


VALID_REQ = {"offer": "o1", "amount_sats": 500}


# --- happy path (sanity for the harness) -------------------------------------

def test_valid_request_issues_invoice() -> None:
    server, calls = _dispatch_server()
    _dispatch(server, _request_event(server, PrivateKey(), VALID_REQ))
    assert calls["issued"] == [(500, None, False)]
    assert calls["responses"] == []


# --- freshness clamps ---------------------------------------------------------

def test_stale_request_dropped() -> None:
    server, calls = _dispatch_server()
    old = int(time.time()) - MAX_REQUEST_AGE_SEC - 5
    _dispatch(server, _request_event(server, PrivateKey(), VALID_REQ, created_at=old))
    assert calls["issued"] == [] and calls["responses"] == []


def test_future_dated_request_dropped() -> None:
    server, calls = _dispatch_server()
    future = int(time.time()) + MAX_CLOCK_SKEW_SEC + 5
    _dispatch(server, _request_event(server, PrivateKey(), VALID_REQ, created_at=future))
    assert calls["issued"] == [] and calls["responses"] == []


def test_expiration_tag_cannot_extend_freshness_window() -> None:
    # Regression: a stale request used to be accepted as long as it carried a
    # not-yet-passed NIP-40 expiration tag — a replaying relay's dream.
    server, calls = _dispatch_server()
    now = int(time.time())
    stale = _request_event(
        server, PrivateKey(), VALID_REQ,
        created_at=now - MAX_REQUEST_AGE_SEC - 5,
        extra_tags=[["expiration", str(now + 3600)]])
    _dispatch(server, stale)
    assert calls["issued"] == [] and calls["responses"] == []


def test_expired_expiration_tag_still_rejects_fresh_event() -> None:
    # The tag may only shrink the window: fresh created_at + passed expiration -> drop.
    server, calls = _dispatch_server()
    now = int(time.time())
    event = _request_event(server, PrivateKey(), VALID_REQ, created_at=now,
                           extra_tags=[["expiration", str(now - 10)]])
    _dispatch(server, event)
    assert calls["issued"] == [] and calls["responses"] == []


# --- replay guard -------------------------------------------------------------

def test_duplicate_event_processed_once() -> None:
    server, calls = _dispatch_server()
    event = _request_event(server, PrivateKey(), VALID_REQ)
    _dispatch(server, event)
    _dispatch(server, event)  # e.g. redelivered by a second relay
    assert calls["issued"] == [(500, None, False)]
    assert calls["responses"] == []  # the replay is dropped silently


def test_junk_events_do_not_pollute_seen_cache() -> None:
    # Undecryptable events (any relay can mass-produce them with throwaway
    # keys) must not enter the bounded seen-cache, where they could evict a
    # real entry and reopen a replay window.
    server, calls = _dispatch_server()
    valid = _request_event(server, PrivateKey(), VALID_REQ)
    _dispatch(server, valid)
    for _ in range(10):
        junk = Event(pubkey=PrivateKey().public_key.hex(), kind=CLINK_EVENT_KIND,
                     created_at=int(time.time()), tags=[["p", server.pubkey_hex]],
                     content="not nip44 at all")
        _dispatch(server, junk)
    assert len(server._seen_events) == 1  # only the decryptable request was recorded
    _dispatch(server, valid)              # ...and its replay is still blocked
    assert calls["issued"] == [(500, None, False)]


# --- request schema validation ------------------------------------------------

@pytest.mark.parametrize("bad_offer", [
    ["o1"],                 # list: used to raise TypeError on the dict lookup
    {"id": "o1"},           # dict: same
    123,
    None,
    "",
    "z" * (protocol.MAX_OFFER_ID_LEN + 1),
])
def test_mistyped_or_oversized_offer_answered_with_error(bad_offer: Any) -> None:
    server, calls = _dispatch_server()
    req = {"offer": bad_offer, "amount_sats": 5}
    _dispatch(server, _request_event(server, PrivateKey(), req))
    assert calls["issued"] == []
    assert len(calls["responses"]) == 1
    assert calls["responses"][0]["code"] == protocol.ERR_INVALID_OFFER


def test_activity_entry_is_bounded_even_for_hostile_offer_ids() -> None:
    server, calls = _dispatch_server()
    req = {"offer": "z" * 64, "amount_sats": 5}  # valid length, unknown offer
    _dispatch(server, _request_event(server, PrivateKey(), req))
    assert all(len(e["offer"]) <= 32 for e in server.recent_activity)


# --- per-payer pending cap ----------------------------------------------------

def test_payer_at_cap_gets_retryable_error() -> None:
    server, calls = _dispatch_server()
    server.receipts = SimpleNamespace(
        pending_count_for=lambda pub: MAX_PENDING_PER_PAYER)
    _dispatch(server, _request_event(server, PrivateKey(), VALID_REQ))
    assert calls["issued"] == []
    assert len(calls["responses"]) == 1
    assert calls["responses"][0]["code"] == protocol.ERR_TEMPORARY_FAILURE


def test_payer_below_cap_is_served() -> None:
    server, calls = _dispatch_server()
    server.receipts = SimpleNamespace(
        pending_count_for=lambda pub: MAX_PENDING_PER_PAYER - 1)
    _dispatch(server, _request_event(server, PrivateKey(), VALID_REQ))
    assert calls["issued"] == [(500, None, False)]


def test_selftest_payer_bypasses_cap() -> None:
    # A self-test issues and immediately unwinds; the cap must not make the
    # "Check noffers" button fail on a busy (or attacked) wallet.
    server, calls = _dispatch_server()
    server.receipts = SimpleNamespace(
        pending_count_for=lambda pub: MAX_PENDING_PER_PAYER)
    payer_sk = PrivateKey()
    server._selftest_payers[payer_sk.public_key.hex()] = time.time() + 60
    _dispatch(server, _request_event(server, payer_sk, VALID_REQ))
    assert calls["issued"] == [(500, None, True)]


# --- error hygiene ------------------------------------------------------------

def test_invoice_failure_reply_is_generic() -> None:
    """A wallet-side exception must not leak its text to the payer."""
    from electrum.logging import Logger
    from clink.liquidity import LiquidityReserver

    server = ClinkServer.__new__(ClinkServer)
    Logger.__init__(server)
    server.config = SimpleNamespace(CLINK_INVOICE_EXPIRY=120)
    server.recent_activity = deque(maxlen=50)
    server.reserver = LiquidityReserver(capacity_fn=lambda: 1000, clock_fn=time.time)

    class _FailingWallet:
        def create_request(self, amount_sat: int, message: str,
                           exp_delay: int, address: Any) -> str:
            raise RuntimeError("secret detail: /home/user/.electrum/wallets/w1")

    server.wallet = _FailingWallet()
    responses: List[Dict[str, Any]] = []

    async def send_response(event: Any, payload: Dict[str, Any]) -> None:
        responses.append(payload)
    server.send_response = send_response  # type: ignore[method-assign]

    event = SimpleNamespace(pubkey="aa" * 32, id="ee" * 32)
    asyncio.run(server._issue_invoice(event, Offer(offer_id="o1"), 5, None))

    assert len(responses) == 1
    assert responses[0]["code"] == protocol.ERR_TEMPORARY_FAILURE
    assert responses[0]["error"] == "Temporary Failure"
    assert "secret" not in json.dumps(responses[0])


# --- wallet-request garbage collection ---------------------------------------

class _GcWallet:
    """Fake wallet: one request with a fixed status, records deletions."""

    def __init__(self, status: Optional[int]) -> None:
        self._status = status  # None -> no such request
        self.deleted: List[str] = []

    def get_request(self, rhash: str) -> Optional[SimpleNamespace]:
        return SimpleNamespace(rhash=rhash) if self._status is not None else None

    def get_invoice_status(self, request: Any) -> int:
        assert self._status is not None
        return self._status

    def delete_request(self, rhash: str) -> None:
        self.deleted.append(rhash)


def _gc_server(status: Optional[int]) -> Tuple[Any, _GcWallet]:
    from electrum.logging import Logger

    server = ClinkServer.__new__(ClinkServer)
    Logger.__init__(server)
    wallet = _GcWallet(status)
    server.wallet = wallet
    return server, wallet


@pytest.mark.parametrize("status", [PR_UNPAID, PR_EXPIRED])
def test_gc_deletes_unpaid_or_expired_request(status: int) -> None:
    server, wallet = _gc_server(status)
    server._delete_stale_request("aa" * 32)
    assert wallet.deleted == ["aa" * 32]


def test_gc_never_deletes_a_paid_request() -> None:
    server, wallet = _gc_server(PR_PAID)
    server._delete_stale_request("aa" * 32)
    assert wallet.deleted == []


def test_gc_tolerates_missing_request() -> None:
    server, wallet = _gc_server(None)
    server._delete_stale_request("aa" * 32)  # must not raise
    assert wallet.deleted == []
