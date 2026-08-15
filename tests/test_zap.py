"""Unit tests for the pure NIP-57 zap logic in :mod:`clink.zap`.

Uses real BIP-340 signatures (the 9734 events are genuinely signed with
``electrum_aionostr`` keys) but no relays or wallet I/O.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from electrum_aionostr.event import Event
from electrum_aionostr.key import PrivateKey

from clink import zap as z
from clink.receipts import ReceiptTarget

R1, R2 = "wss://relay.one", "wss://relay.two"


def _sign(zap: Dict[str, Any], sender: PrivateKey) -> Dict[str, Any]:
    ev = Event(
        pubkey=sender.public_key.hex(), created_at=zap["created_at"],
        kind=zap["kind"], tags=zap["tags"], content=zap.get("content", ""),
    ).sign(sender.hex())
    zap["id"] = ev.id
    zap["sig"] = ev.sig
    return zap


def _zap_request(*, recipient: str, sender: Optional[PrivateKey] = None,
                 amount_msat: Optional[int] = 5000,
                 created_at: Optional[int] = None,
                 content: str = "Nice post!",
                 tags_extra: Optional[List[List[str]]] = None,
                 relays: Tuple[str, ...] = (R1, R2),
                 kind: int = z.ZAP_REQUEST_KIND,
                 **overrides: Any) -> Tuple[PrivateKey, Dict[str, Any]]:
    sender = sender or PrivateKey()
    tags: List[List[str]] = []
    if relays:
        tags.append(["relays", *relays])
    if amount_msat is not None:
        tags.append(["amount", str(amount_msat)])
    tags.append(["p", recipient])
    tags.extend(tags_extra or [])
    zap = {
        "id": "", "pubkey": sender.public_key.hex(),
        "created_at": int(created_at if created_at is not None else time.time()),
        "kind": kind, "tags": tags, "content": content, "sig": "",
    }
    zap.update(overrides)
    if kind == z.ZAP_REQUEST_KIND:
        zap = _sign(zap, sender)
    return sender, zap


def _raw(sender: PrivateKey, zap: Dict[str, Any]) -> str:
    return json.dumps(zap)


# --- parse_zap_request -------------------------------------------------------

def test_parse_accepts_stringified_object() -> None:
    _, zap = _zap_request(recipient="a" * 64)
    parsed = z.parse_zap_request(json.dumps(zap))
    assert parsed is not None and parsed["kind"] == z.ZAP_REQUEST_KIND


def test_parse_rejects_junk() -> None:
    assert z.parse_zap_request("not json{") is None
    assert z.parse_zap_request("[1, 2]") is None
    assert z.parse_zap_request("") is None
    assert z.parse_zap_request(123) is None  # type: ignore[arg-type]
    assert z.parse_zap_request(None) is None  # type: ignore[arg-type]


# --- structural validation ---------------------------------------------------

def test_valid_zap_request_passes() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient)
    assert z.zap_request_error(zap, expected_recipient=recipient) is None


def test_valid_zap_with_e_tag_passes() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(
        recipient=recipient,
        tags_extra=[["e", "bb" * 32], ["k", "1"]])
    assert z.zap_request_error(zap, expected_recipient=recipient) is None


def test_valid_zap_with_a_tag_passes() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(
        recipient=recipient,
        tags_extra=[["a", f"30023:{'cc' * 32}:short-id"], ["k", "30023"]])
    assert z.zap_request_error(zap, expected_recipient=recipient) is None


def test_rejects_wrong_kind() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient, kind=1)
    assert z.zap_request_error(zap, expected_recipient=recipient) == "not a zap request"


def test_rejects_stale_and_future_dated() -> None:
    recipient = "aa" * 32
    now = int(time.time())
    _, zap = _zap_request(recipient=recipient, created_at=now - 120)
    assert z.zap_request_error(zap, now=now, expected_recipient=recipient) == "zap request is stale"
    _, zap = _zap_request(recipient=recipient, created_at=now + 120)
    assert z.zap_request_error(zap, now=now, expected_recipient=recipient) == "zap request is stale"


def test_rejects_tampered_signature() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient)
    zap["content"] = "forged"
    assert z.zap_request_error(zap, expected_recipient=recipient) == "invalid zap signature"


def test_rejects_forged_id() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient)
    zap["id"] = "00" * 32
    assert z.zap_request_error(zap, expected_recipient=recipient) == "invalid zap signature"


def test_rejects_wrong_recipient() -> None:
    _, zap = _zap_request(recipient="aa" * 32)
    assert (z.zap_request_error(zap, expected_recipient="bb" * 32)
            == "zap recipient does not match this service")


def test_rejects_missing_or_duplicate_p_tag() -> None:
    _, zap = _zap_request(recipient="aa" * 32, tags_extra=[["p", "bb" * 32]])
    assert z.zap_request_error(zap) == "zap request must have exactly one p tag"


def test_rejects_too_many_e_tags() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient,
                          tags_extra=[["e", "bb" * 32], ["e", "cc" * 32]])
    assert z.zap_request_error(zap) == "zap request has too many e tags"


def test_rejects_malformed_e_tag() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient, tags_extra=[["e", "zz"]])
    assert z.zap_request_error(zap) == "invalid e tag"


def test_rejects_malformed_a_coordinate() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient, tags_extra=[["a", "not:coord"]])
    assert z.zap_request_error(zap) == "invalid a tag"


def test_rejects_malformed_k_tag() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient, tags_extra=[["k", "not-a-kind"]])
    assert z.zap_request_error(zap) == "invalid k tag"


def test_rejects_mismatched_P_tag() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient, tags_extra=[["P", "bb" * 32]])
    assert z.zap_request_error(zap) == "invalid P tag"


def test_P_tag_matching_sender_passes() -> None:
    sender, zap = _zap_request(recipient="aa" * 32,
                               tags_extra=[["P", ""]])
    zap["tags"] = [t for t in zap["tags"] if t[0] != "P"] + [["P", sender.public_key.hex()]]
    zap = _sign(zap, sender)
    assert z.zap_request_error(zap) is None


def test_rejects_no_relays() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient, relays=())
    assert z.zap_request_error(zap) == "zap request names no relays"


def test_rejects_malformed_amount_tag() -> None:
    recipient = "aa" * 32
    sender = PrivateKey()
    zap = {
        "id": "", "pubkey": sender.public_key.hex(),
        "created_at": int(time.time()), "kind": z.ZAP_REQUEST_KIND,
        "tags": [["relays", R1], ["amount", "12.5"], ["p", recipient]],
        "content": "", "sig": "",
    }
    zap = _sign(zap, sender)
    assert z.zap_request_error(zap) == "invalid zap amount"


def test_absent_amount_tag_is_fine() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient, amount_msat=None)
    assert z.zap_request_error(zap) is None


# --- amount extraction -------------------------------------------------------

def test_amount_sat_rounds_up_from_millisats() -> None:
    _, zap = _zap_request(recipient="aa" * 32, amount_msat=1500)
    assert z.zap_amount_msat(zap) == 1500
    assert z.zap_amount_sat(zap) == 2


def test_amount_missing_returns_none() -> None:
    _, zap = _zap_request(recipient="aa" * 32, amount_msat=None)
    assert z.zap_amount_msat(zap) is None
    assert z.zap_amount_sat(zap) is None


def test_amount_malformed_returns_none() -> None:
    _, zap = _zap_request(recipient="aa" * 32, amount_msat=None)
    zap["tags"].append(["amount", "many"])
    assert z.zap_amount_msat(zap) is None


def test_relays_and_memo() -> None:
    _, zap = _zap_request(recipient="aa" * 32, content="hello")
    assert z.zap_relays(zap) == [R1, R2]
    assert z.zap_memo(zap) == "hello"
    assert z.zap_recipient(zap) == "aa" * 32
    assert z.zap_sender(zap) == zap["pubkey"]
    assert z.zap_event_id(zap) == zap["id"]


# --- invoice amount resolution ------------------------------------------------

def test_zap_amount_is_authoritative() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient, amount_msat=21000)  # 21 sat
    sats, err = z.zap_invoice_amount_sat({"offer": "o1"}, zap)
    assert err is None and sats == 21


def test_agreement_between_zap_and_request_amount() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient, amount_msat=21000)
    sats, err = z.zap_invoice_amount_sat({"offer": "o1", "amount_sats": 21}, zap)
    assert err is None and sats == 21


def test_conflicting_amounts_answer_invalid_amount() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient, amount_msat=21000)
    sats, err = z.zap_invoice_amount_sat({"offer": "o1", "amount_sats": 99}, zap)
    assert sats is None
    assert err is not None and err.get("code") == 5
    assert err["range"] == {"min": 21, "max": 99}


def test_request_amount_fills_in_missing_zap_amount() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient, amount_msat=None)
    sats, err = z.zap_invoice_amount_sat({"offer": "o1", "amount_sats": 42}, zap)
    assert err is None and sats == 42


def test_no_amount_anywhere_returns_none() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient, amount_msat=None)
    sats, err = z.zap_invoice_amount_sat({"offer": "o1"}, zap)
    assert sats is None and err is None


# --- receipt construction ----------------------------------------------------

def test_receipt_tags_carry_zap_context() -> None:
    recipient = "aa" * 32
    eid, preimage = "bb" * 32, "cc" * 32
    sender, zap = _zap_request(
        recipient=recipient,
        tags_extra=[["e", eid], ["k", "1"]])
    raw = json.dumps(zap)
    tags = z.zap_receipt_tags(zap, raw, "lnbcrt1x", preimage)
    d = {t[0]: t for t in tags}
    assert d["p"][1] == recipient
    assert d["P"][1] == zap["pubkey"]
    assert d["e"][1] == eid
    assert d["k"][1] == "1"
    assert d["bolt11"][1] == "lnbcrt1x"
    assert d["description"][1] == raw
    assert d["preimage"][1] == preimage


def test_receipt_tags_without_preimage() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient)
    tags = z.zap_receipt_tags(zap, json.dumps(zap), "lnbcrt1x", None)
    assert all(t[0] != "preimage" for t in tags)


def test_receipt_from_target() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient)
    raw = json.dumps(zap)
    target = ReceiptTarget(rhash="r", payer_pubkey="p", request_event_id="e", zap=raw)
    assert z.zap_receipt_from_target(target) == zap
    assert z.zap_receipt_from_target(ReceiptTarget(
        rhash="r", payer_pubkey="p", request_event_id="e")) is None


# --- ClinkServer._publish_zap_receipt -----------------------------------------

def _receipt_server() -> Tuple[Any, List[Any], List[str]]:
    """A ``ClinkServer`` shell whose ``_publish_event_to`` records relays."""
    from collections import deque

    from electrum.logging import Logger

    from clink.clink_plugin import ClinkServer

    server = ClinkServer.__new__(ClinkServer)
    Logger.__init__(server)
    server_sk = PrivateKey()
    server.private_key = server_sk
    server.pubkey_hex = server_sk.public_key.hex()
    server.ssl_context = None
    server.recent_activity = deque(maxlen=50)
    sent: List[str] = []
    server.receipts = SimpleNamespace(
        mark_sent=lambda rhash: sent.append(rhash),
        record_attempt=lambda rhash: None,
        sweep=lambda: [], due_targets=lambda: [],
    )
    received: List[Any] = []
    relays_seen: List[str] = []

    async def publish_event_to(relay: str, event: Any, factory: Any) -> None:
        relays_seen.append(relay)
        received.append(event)

    server._publish_event_to = publish_event_to  # type: ignore[method-assign]
    server._proxy_factory = lambda: None  # type: ignore[method-assign]
    return server, received, sent


def test_publish_zap_receipt_publishes_to_named_relays() -> None:
    from clink.receipts import ReceiptTarget

    recipient = "aa" * 32
    sender, zap = _zap_request(
        recipient=recipient,
        tags_extra=[["e", "bb" * 32], ["k", "1"]])
    raw = json.dumps(zap)
    target = ReceiptTarget(
        rhash="r" * 64, payer_pubkey=sender.public_key.hex(),
        request_event_id="e" * 64, due_since=12345.0,
        preimage="cc" * 32, zap=raw, bolt11="lnbcrt1x")
    server, received, sent = _receipt_server()

    ok = asyncio.run(server._publish_zap_receipt(target))
    assert ok is True
    assert sent == ["r" * 64]
    assert len(received) == 2
    receipt = received[0]
    assert receipt.kind == z.ZAP_RECEIPT_KIND
    assert receipt.pubkey == server.pubkey_hex
    assert receipt.content == ""
    assert receipt.created_at == 12345
    # Validly signed by the service.
    assert Event(pubkey=receipt.pubkey, created_at=receipt.created_at,
                 kind=receipt.kind, tags=receipt.tags,
                 content=receipt.content, sig=receipt.sig).id == receipt.id
    d = {t[0]: t for t in receipt.tags}
    assert d["p"][1] == recipient
    assert d["P"][1] == zap["pubkey"]
    assert d["e"][1] == "bb" * 32
    assert d["bolt11"][1] == "lnbcrt1x"
    assert d["description"][1] == raw
    assert d["preimage"][1] == "cc" * 32
    # Both named relays were used.
    assert received[0].tags == received[1].tags


def test_publish_zap_receipt_without_relays_drops_entry() -> None:
    recipient = "aa" * 32
    _, zap = _zap_request(recipient=recipient, relays=())
    raw = json.dumps(zap)
    server, received, sent = _receipt_server()
    target = ReceiptTarget(rhash="r" * 64, payer_pubkey="p",
                           request_event_id="e", zap=raw, bolt11="lnbcrt1x")
    ok = asyncio.run(server._publish_zap_receipt(target))
    assert ok is True
    assert received == []          # nothing published
    assert sent == ["r" * 64]      # owed-for-nothing entry dropped


def test_publish_zap_receipt_with_broken_zap_drops_entry() -> None:
    server, received, sent = _receipt_server()
    target = ReceiptTarget(rhash="r" * 64, payer_pubkey="p",
                           request_event_id="e", zap="not json")
    ok = asyncio.run(server._publish_zap_receipt(target))
    assert ok is True
    assert received == []
    assert sent == ["r" * 64]
