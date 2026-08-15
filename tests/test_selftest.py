"""Unit tests for the noffer round-trip self-test ("Check noffers").

Covers three independently-testable layers with no network:

  * ``_run_check`` — the connect -> subscribe -> publish -> response ordering
    and outcome classification, driven by an in-memory fake relay hub plus a
    fake *service* that answers requests with real NIP-44 crypto both ways.
  * ``check_noffer`` — noffer decoding, payer registration bracketing, and the
    unreachable-relay path (a real ``aionostr.Manager`` against a closed port).
  * ``ClinkServer`` self-test hooks — the payer registry TTL, and the critical
    guarantee that a self-test invoice leaves no side effects behind (no
    liquidity lock, no wallet request, no dev-fee or receipt bookkeeping)
    while a normal request keeps all of them.

Following the repo convention (no pytest-asyncio), each test drives its
coroutine with ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from electrum_aionostr.key import PrivateKey

from clink import nip44
from clink.noffer import Noffer, OfferPriceType, noffer_encode
from clink.selftest import (
    SELFTEST_AMOUNT_SAT,
    CheckResult,
    CheckStatus,
    _run_check,
    check_noffer,
)

# --- fakes -----------------------------------------------------------------

class _FakeEvent:
    def __init__(self, pubkey: str, content: str, tags: List[List[str]]) -> None:
        self.pubkey = pubkey
        self.content = content
        self.tags = tags


class _FakeRelayHub:
    """A minimal in-memory relay: delivers a published event only to a live
    subscriber (ephemeral semantics — no storage, live subscribers only)."""

    def __init__(self) -> None:
        self._subscribers: List[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def publish(self, event: _FakeEvent) -> None:
        for q in self._subscribers:
            q.put_nowait(event)


class _FakeManager:
    def __init__(self, hub: Optional[_FakeRelayHub], *, reachable: bool = True,
                 hang_connect: bool = False) -> None:
        self._hub = hub
        self._reachable = reachable
        self._hang_connect = hang_connect
        self.connected = False
        self.relays = ["r"]
        self.closed = False

    async def connect(self) -> None:
        if self._hang_connect:
            await asyncio.sleep(3600)
        if not self._reachable:
            self.relays = []           # matches Manager dropping unreachable relays
            self.connected = True
            return
        self.connected = True

    async def get_events(self, *filters, single_event=False, only_stored=False):
        assert self._hub is not None
        q = self._hub.subscribe()
        while True:
            ev = await q.get()
            yield ev
            if single_event:
                break

    async def close(self) -> None:
        self.closed = True


class _FakeService:
    """The receiver side: decrypts the payer's request off the hub and replies
    like the real plugin — an encrypted kind-21001 event tagged p+e."""

    def __init__(self, hub: _FakeRelayHub) -> None:
        self.hub = hub
        self.sk = PrivateKey()
        self.pub = self.sk.public_key.hex()

    def noffer_for(self, offer_id: str = "offer-1", relay: str = "ws://fake") -> str:
        return noffer_encode(Noffer(
            pubkey=self.pub, relay=relay, offer=offer_id,
            price_type=OfferPriceType.SPONTANEOUS))

    def reply(self, payer_pub: str, request_id: str, payload: Dict[str, Any],
              *, from_pubkey: Optional[str] = None,
              garble: bool = False, drop_etag: bool = False) -> None:
        content = nip44.encrypt_to(self.sk.raw_secret, payer_pub, json.dumps(payload))
        if garble:
            content = "not-nip44-at-all"
        tags = [["p", payer_pub]] + ([] if drop_etag else [["e", request_id]])
        self.hub.publish(_FakeEvent(from_pubkey or self.pub, content, tags))


def _drive(service: _FakeService, manager: _FakeManager, payer_sk: PrivateKey,
           respond: Callable[[str, str], None],
           *,
           offer_id: str = "offer-1",
           validate_bolt11=None,
           timeout: float = 2.0,
           publish_raises: bool = False) -> CheckResult:
    """Run ``_run_check`` against the fake hub with ``respond(payer_pub, request_id)``
    invoked right after the (fake) publish, mimicking the service answering."""
    noffer_str = service.noffer_for(offer_id)
    noffer = Noffer(pubkey=service.pub, relay="ws://fake", offer=offer_id)
    payer_pub = payer_sk.public_key.hex()

    async def publish() -> str:
        if publish_raises:
            raise RuntimeError("relay said no")
        request_id = "req-" + payer_pub[:8]
        # Give the response task a beat to be subscribed, as on a real relay.
        asyncio.get_running_loop().call_soon(respond, payer_pub, request_id)
        return request_id

    return asyncio.run(_run_check(
        noffer, noffer_str, manager,
        payer_sk=payer_sk, publish=publish,
        validate_bolt11=validate_bolt11, timeout=timeout, settle=0.0))


# --- _run_check: classification ---------------------------------------------

def test_ok_when_valid_invoice_comes_back() -> None:
    hub = _FakeRelayHub()
    service = _FakeService(hub)
    payer_sk = PrivateKey()
    seen: List[Tuple[str, int]] = []

    def validator(bolt11: str, amount_sat: int) -> Optional[str]:
        seen.append((bolt11, amount_sat))
        return None

    res = _drive(service, _FakeManager(hub), payer_sk,
                 lambda pub, rid: service.reply(pub, rid, {"bolt11": "lnbcrt1fake"}),
                 validate_bolt11=validator)
    assert res.status is CheckStatus.OK and res.ok
    assert res.rtt_ms is not None
    assert seen == [("lnbcrt1fake", SELFTEST_AMOUNT_SAT)]


def test_bad_response_when_invoice_fails_validation() -> None:
    hub = _FakeRelayHub()
    service = _FakeService(hub)
    res = _drive(service, _FakeManager(hub), PrivateKey(),
                 lambda pub, rid: service.reply(pub, rid, {"bolt11": "lnbcrt1fake"}),
                 validate_bolt11=lambda b, a: "wrong amount")
    assert res.status is CheckStatus.BAD_RESPONSE
    assert "wrong amount" in res.detail


def test_listener_error_reply_is_classified_with_code() -> None:
    hub = _FakeRelayHub()
    service = _FakeService(hub)
    res = _drive(service, _FakeManager(hub), PrivateKey(),
                 lambda pub, rid: service.reply(
                     pub, rid, {"code": 5, "error": "Invalid Amount",
                                "range": {"min": 1, "max": 0}}))
    assert res.status is CheckStatus.LISTENER_ERROR
    assert res.error_code == 5
    assert "Invalid Amount" in res.detail


def test_unreachable_relay() -> None:
    hub = _FakeRelayHub()
    service = _FakeService(hub)
    res = _drive(service, _FakeManager(None, reachable=False), PrivateKey(),
                 lambda pub, rid: None)
    assert res.status is CheckStatus.UNREACHABLE


def test_connect_timeout_is_unreachable() -> None:
    hub = _FakeRelayHub()
    service = _FakeService(hub)
    res = _drive(service, _FakeManager(hub, hang_connect=True), PrivateKey(),
                 lambda pub, rid: None, timeout=0.2)
    assert res.status is CheckStatus.UNREACHABLE
    assert "timed out" in res.detail


def test_publish_failure() -> None:
    hub = _FakeRelayHub()
    service = _FakeService(hub)
    manager = _FakeManager(hub)
    res = _drive(service, manager, PrivateKey(),
                 lambda pub, rid: None, publish_raises=True)
    assert res.status is CheckStatus.PUBLISH_FAILED
    assert manager.closed  # cleaned up even on failure


def test_no_response_times_out() -> None:
    hub = _FakeRelayHub()
    service = _FakeService(hub)
    manager = _FakeManager(hub)
    res = _drive(service, manager, PrivateKey(),
                 lambda pub, rid: None, timeout=0.3)
    assert res.status is CheckStatus.NO_RESPONSE
    assert manager.closed


def test_foreign_and_garbled_events_are_skipped() -> None:
    # Wrong-author, undecryptable, and un-tagged events must not end the wait;
    # the genuine response after them must still be found.
    hub = _FakeRelayHub()
    service = _FakeService(hub)

    def respond(pub: str, rid: str) -> None:
        service.reply(pub, rid, {"bolt11": "x"}, from_pubkey="ff" * 32)  # wrong author
        service.reply(pub, rid, {"bolt11": "x"}, garble=True)            # bad ciphertext
        service.reply(pub, rid, {"bolt11": "x"}, drop_etag=True)         # not our request
        service.reply(pub, rid, {"bolt11": "lnbcrt1real"})               # the real one

    res = _drive(service, _FakeManager(hub), PrivateKey(), respond,
                 validate_bolt11=lambda b, a: None if b == "lnbcrt1real" else "bogus")
    assert res.status is CheckStatus.OK


def test_response_with_neither_invoice_nor_error() -> None:
    hub = _FakeRelayHub()
    service = _FakeService(hub)
    res = _drive(service, _FakeManager(hub), PrivateKey(),
                 lambda pub, rid: service.reply(pub, rid, {"res": "ok"}))
    assert res.status is CheckStatus.BAD_RESPONSE


# --- check_noffer: decoding + registration bracketing ------------------------

def test_invalid_noffer_string() -> None:
    res = asyncio.run(check_noffer("noffer1notavalidstring"))
    assert res.status is CheckStatus.INVALID_NOFFER


def test_not_a_noffer_at_all() -> None:
    res = asyncio.run(check_noffer("lnbc1xyz"))
    assert res.status is CheckStatus.INVALID_NOFFER


def test_unreachable_real_manager_registers_and_unregisters_payer() -> None:
    # A real aionostr.Manager against a port nothing listens on: the check must
    # come back UNREACHABLE and the payer identity must be bracketed correctly.
    sk = PrivateKey()
    noffer_str = noffer_encode(Noffer(
        pubkey=sk.public_key.hex(), relay="ws://127.0.0.1:1", offer="o"))
    registered: List[str] = []
    unregistered: List[str] = []

    res = asyncio.run(check_noffer(
        noffer_str, timeout=3.0,
        register_payer=registered.append,
        unregister_payer=unregistered.append,
    ))
    assert res.status is CheckStatus.UNREACHABLE
    assert len(registered) == 1 and registered == unregistered


# --- ClinkServer hooks: payer registry + side-effect unwinding ---------------

def _bare_server():
    """A ClinkServer shell with just enough state for the self-test hooks.

    Built without __init__ (no wallet/network); each test attaches exactly the
    collaborators the method under test touches.
    """
    from electrum.logging import Logger

    from clink.clink_plugin import ClinkServer

    server = ClinkServer.__new__(ClinkServer)
    Logger.__init__(server)
    server._selftest_payers = {}
    return server


def test_selftest_payer_registry_and_ttl() -> None:
    server = _bare_server()
    assert not server.is_selftest_payer("aa")
    server.register_selftest_payer("aa")
    assert server.is_selftest_payer("aa")
    server.unregister_selftest_payer("aa")
    assert not server.is_selftest_payer("aa")
    # Crash safety: an entry that was never unregistered expires on its own.
    server.register_selftest_payer("bb")
    server._selftest_payers["bb"] = time.time() - 1
    assert not server.is_selftest_payer("bb")
    assert "bb" not in server._selftest_payers  # pruned, not just hidden


class _RecordingWallet:
    def __init__(self) -> None:
        self.deleted: List[str] = []
        self.request = SimpleNamespace(payment_hash=b"\x11" * 32, rhash="11" * 32)
        self.lnworker = SimpleNamespace(
            get_payment_info=lambda payment_hash, direction: SimpleNamespace(),
            get_bolt11_invoice=lambda payment_info, message, fallback_address: (None, "lnbcrt1selftest"),
        )

    def create_request(self, amount_sat: int, message: str, exp_delay: int, address) -> str:
        return "reqkey"

    def get_request(self, key: str):
        return self.request

    def delete_request(self, key: str) -> None:
        self.deleted.append(key)


def _issue_server() -> Tuple[Any, _RecordingWallet, Dict[str, Any]]:
    from collections import deque

    from clink.liquidity import LiquidityReserver

    server = _bare_server()
    wallet = _RecordingWallet()
    calls: Dict[str, Any] = {"devfee": [], "receipts": [], "responses": []}
    server.wallet = wallet
    server.config = SimpleNamespace(CLINK_INVOICE_EXPIRY=120)
    server.recent_activity = deque(maxlen=50)
    server.reserver = LiquidityReserver(capacity_fn=lambda: 1000, clock_fn=time.time)
    server.devfee = SimpleNamespace(mark_issued=lambda rhash: calls["devfee"].append(rhash))
    server.receipts = SimpleNamespace(
        remember=lambda rhash, pub, eid, expires_at: calls["receipts"].append(rhash))

    async def send_response(event, payload):
        calls["responses"].append(payload)
    server.send_response = send_response
    return server, wallet, calls


def _request_event() -> Any:
    return SimpleNamespace(pubkey="aa" * 32, id="ee" * 32)


def test_selftest_invoice_leaves_no_side_effects() -> None:
    from clink.offers import Offer

    server, wallet, calls = _issue_server()
    asyncio.run(server._issue_invoice(
        _request_event(), Offer(offer_id="o1", label="L"), 1, None, selftest=True))

    assert calls["responses"] and "bolt11" in calls["responses"][0]  # real invoice went out
    assert calls["devfee"] == []                     # no dev-fee bookkeeping
    assert calls["receipts"] == []                   # no receipt owed
    assert server.reserver.active() == []            # liquidity lock released
    assert wallet.deleted == ["reqkey"]              # wallet request removed
    assert any("self-test" in e["result"] for e in server.recent_activity)


def test_normal_invoice_keeps_side_effects() -> None:
    from clink.offers import Offer

    server, wallet, calls = _issue_server()
    asyncio.run(server._issue_invoice(
        _request_event(), Offer(offer_id="o1", label="L"), 5, None, selftest=False))

    assert calls["responses"] and "bolt11" in calls["responses"][0]
    assert calls["devfee"] == ["11" * 32]            # dev fee armed
    assert calls["receipts"] == ["11" * 32]          # receipt remembered
    assert [r.amount_sat for r in server.reserver.active()] == [5]  # lock held
    assert wallet.deleted == []                      # request kept
