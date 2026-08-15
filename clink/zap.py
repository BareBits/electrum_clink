"""NIP-57 zap support for CLINK offers (pure, no relay/wallet I/O).

CLINK offers can stand in for the LNURL-pay callback step of the NIP-57 zap
flow: a payer sends a kind-21001 request whose ``zap`` field carries the full
kind-9734 *zap request* event (stringified JSON), we validate it, invoice the
zap amount, and — once the invoice is paid — publish a kind-9735 *zap receipt*
to the relays the payer named. This module holds everything about a 9734 that
the runtime needs, kept free of I/O so the policy is unit-testable.

Wire shape (spec "NIP-57 Zaps" + NIP-57):

    Request (payer -> us):   {..., "zap": "<stringified kind-9734 event>"}
    Receipt (us -> relays):  kind 9735, content "", tags p/P/e/a/k/bolt11/
                             description/preimage; published to the 9734's
                             ``relays`` tag.

Amounts: NIP-57 names millisats; CLINK invoices are in whole sats, so the zap
amount rounds *up* (ceil) — a 1500 msat zap mints a 2-sat invoice, never a
shortfall on a partial-sat amount.

The bolt11 we issue is our own wallet's invoice (memo from the offer, not the
9734), so ``SHA256(description)`` will not match its description hash — NIP-57
only *recommends* that and the description hash is beyond our control here; the
``description`` tag still carries the payer's verbatim 9734 for clients.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import protocol
from .receipts import ReceiptTarget

# NIP-57 event kinds.
ZAP_REQUEST_KIND = 9734
ZAP_RECEIPT_KIND = 9735

# Upper bound for a zap amount in millisats (whole bitcoin supply).
MAX_ZAP_MSAT = protocol.MAX_AMOUNT_SAT * 1000
# Sanity cap for the zap memo folded into logs/activity only.
MAX_ZAP_CONTENT_LEN = 1000
# Freshness window for the 9734, mirroring the kind-21001 request clamps
# (a zap request is created right before the CLINK request that carries it).
ZAP_MAX_AGE_SEC = 60
ZAP_MAX_CLOCK_SKEW_SEC = 60


@dataclass
class ZapRequest:
    """A validated kind-9734 zap request extracted from a CLINK request.

    ``raw`` is the payer's verbatim stringified event, preserved so the zap
    receipt's ``description`` tag carries exactly what the payer signed.
    """

    raw: str
    event_id: str
    sender: str          # 9734 pubkey — who is paying
    recipient: str       # the single ``p`` tag — who is being zapped (us)
    amount_msat: int
    relays: List[str]    # relays the 9735 receipt must be published to
    target_tags: List[List[str]]  # ``e``/``a``/``k`` tags forwarded to the receipt
    content: str         # the payer's memo

    @property
    def amount_sat(self) -> int:
        """The invoice amount in sats: millisats rounded up."""
        return -(-self.amount_msat // 1000)


def parse_zap_request(raw: str) -> Optional[Dict[str, Any]]:
    """Parse the stringified ``zap`` field into a JSON object.

    ``None`` when it is not a JSON object (including junk or a non-dict).
    """
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _tag(zap: Dict[str, Any], name: str) -> List[List[str]]:
    """Every ``[name, ...]`` tag in ``zap``, tolerating malformed tag tables."""
    tags = zap.get("tags")
    if not isinstance(tags, list):
        return []
    out = []
    for t in tags:
        if isinstance(t, list) and t and isinstance(t[0], str) and t[0] == name:
            out.append([str(x) for x in t])
    return out


def _single_value(zap: Dict[str, Any], name: str) -> Optional[str]:
    """The second element of the sole ``[name, ...]`` tag, or ``None``."""
    rows = _tag(zap, name)
    if len(rows) != 1 or len(rows[0]) < 2:
        return None
    return rows[0][1]


def zap_event_id(zap: Dict[str, Any]) -> Optional[str]:
    value = zap.get("id")
    return value if isinstance(value, str) else None


def zap_sender(zap: Dict[str, Any]) -> Optional[str]:
    value = zap.get("pubkey")
    return value if isinstance(value, str) else None


def zap_recipient(zap: Dict[str, Any]) -> Optional[str]:
    """The single ``p`` tag (who is being zapped); None unless exactly one."""
    return _single_value(zap, "p")


def zap_amount_msat(zap: Dict[str, Any]) -> Optional[int]:
    """The ``amount`` tag in millisats, or ``None`` when absent or malformed.

    NIP-57 says the tag is optional; CLINK needs an amount for the invoice, so
    ``None`` here means the request's own ``amount_sats`` may still supply it.
    A *present-but-malformed* amount is treated as invalid by
    :func:`zap_request_error` (never silently invoiced from a broken number).
    """
    value = _single_value(zap, "amount")
    if value is None:
        return None
    if not (value.isascii() and value.isdigit()) or len(value) > 16:
        return None
    msat = int(value)
    if not (0 < msat <= MAX_ZAP_MSAT):
        return None
    return msat


def zap_amount_sat(zap: Dict[str, Any]) -> Optional[int]:
    msat = zap_amount_msat(zap)
    return None if msat is None else -(-msat // 1000)


def zap_relays(zap: Dict[str, Any]) -> List[str]:
    """The relay URLs the payer wants the 9735 receipt published to."""
    relays = []
    for t in _tag(zap, "relays"):
        for r in t[1:]:
            if isinstance(r, str) and r.strip():
                relays.append(r.strip())
    return relays


def zap_target_tags(zap: Dict[str, Any]) -> List[List[str]]:
    """The ``e``/``a``/``k`` tags a 9735 receipt forwards from the request."""
    out: List[List[str]] = []
    for name in ("e", "a", "k"):
        for t in _tag(zap, name):
            # NIP-57: at most one of e and a; forwarding a malformed one would
            # poison the receipt, so keep only well-formed rows.
            if name == "k" and len(t) < 2:
                continue
            out.append(t[:3])
    return out


def zap_memo(zap: Dict[str, Any]) -> str:
    value = zap.get("content")
    return value[:MAX_ZAP_CONTENT_LEN] if isinstance(value, str) else ""


def _is_hex(value: Any, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(
        c in "0123456789abcdefABCDEF" for c in value)


def _valid_a_coordinate(value: Any) -> bool:
    """A NIP-01 addressable coordinate: ``<kind>:<pubkey>:<d-identifier>``."""
    if not isinstance(value, str) or ":" not in value:
        return False
    parts = value.split(":")
    if not parts[0].isdigit() or not (1 <= int(parts[0]) <= 65535):
        return False
    return len(parts) >= 2 and _is_hex(parts[1], 64)


def _verify_signature(zap: Dict[str, Any]) -> bool:
    """Verify the 9734's BIP-340 signature and self-referential event id.

    Rebuilding the aionostr ``Event`` recomputes the id from fields and raises
    on a bad signature, so a forged request can never pass. The JSON ``id`` is
    compared against the recomputed one (the ``Event`` ctor trusts its input id).
    """
    try:
        from electrum_aionostr.event import Event, InvalidEvent
    except Exception:
        return False
    try:
        ev = Event(
            pubkey=zap["pubkey"], created_at=zap["created_at"], kind=zap["kind"],
            tags=zap["tags"], content=zap.get("content", ""),
            sig=zap.get("sig"),
        )
    except (InvalidEvent, TypeError, ValueError, KeyError):
        return False
    return ev.id == zap.get("id")


def zap_request_error(zap: Dict[str, Any], *,
                      now: Optional[int] = None,
                      expected_recipient: Optional[str] = None) -> Optional[str]:
    """Structural validation of a kind-9734 zap request (NIP-57 Appendix D).

    Returns ``None`` when the request is acceptable, else a short message. The
    signature check needs the BIP-340 machinery but is still pure (no I/O).

    Checks, adapted for CLINK: valid kind, signature, id, and ``created_at``
    freshness; exactly one ``p`` tag; at most one ``e`` and one ``a``; a
    well-formed ``k``; at most one ``P`` (equal to the sender, per NIP-57);
    at least one ``relays`` relay (we must publish the receipt somewhere); and a
    well-formed ``amount`` tag when present. ``expected_recipient`` pins the
    ``p`` tag to our service pubkey (the noffer's pubkey is who is zapped).
    """
    if zap.get("kind") != ZAP_REQUEST_KIND:
        return "not a zap request"
    created_at = zap.get("created_at")
    if not isinstance(created_at, int) or isinstance(created_at, bool):
        return "invalid timestamp"
    now = int(now if now is not None else time.time())
    if not (now - ZAP_MAX_AGE_SEC <= created_at <= now + ZAP_MAX_CLOCK_SKEW_SEC):
        return "zap request is stale"
    if not _is_hex(zap.get("pubkey"), 64):
        return "invalid zap sender"
    if not _verify_signature(zap):
        return "invalid zap signature"
    recipient = zap_recipient(zap)
    if recipient is None or not _is_hex(recipient, 64):
        return "zap request must have exactly one p tag"
    if expected_recipient is not None and recipient != expected_recipient:
        return "zap recipient does not match this service"
    if len(_tag(zap, "e")) > 1:
        return "zap request has too many e tags"
    for t in _tag(zap, "e"):
        if len(t) < 2 or not _is_hex(t[1], 64):
            return "invalid e tag"
    if len(_tag(zap, "a")) > 1:
        return "zap request has too many a tags"
    for t in _tag(zap, "a"):
        if len(t) < 2 or not _valid_a_coordinate(t[1]):
            return "invalid a tag"
    for t in _tag(zap, "k"):
        if len(t) < 2 or not (t[1].isdigit() and int(t[1]) <= 65535):
            return "invalid k tag"
    for t in _tag(zap, "P"):
        if len(t) < 2 or not _is_hex(t[1], 64) or t[1] != zap.get("pubkey"):
            return "invalid P tag"
    if not zap_relays(zap):
        return "zap request names no relays"
    if _tag(zap, "amount") and zap_amount_msat(zap) is None:
        return "invalid zap amount"
    return None


def zap_invoice_amount_sat(req: Dict[str, Any],
                           zap: Dict[str, Any]) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    """The sats to invoice for a zap-bearing CLINK request.

    The 9734's ``amount`` tag (millisats, rounded up to sats) is authoritative
    when present; otherwise the request's own ``amount_sats`` supplies the
    amount. When both are present they must agree — answering an invalid-amount
    error whose range spans the conflicting values. Returns ``(sats, None)`` on
    success, ``(None, error_payload)`` on conflict, or ``(None, None)`` when
    neither field supplies an amount (the caller answers invalid-amount).
    """
    zap_sat = zap_amount_sat(zap)
    named = protocol.request_amount_sat(req)
    if zap_sat is not None and named is not None and zap_sat != named:
        lo, hi = min(zap_sat, named), max(zap_sat, named)
        return None, protocol.invalid_amount_payload(lo, hi)
    if zap_sat is not None:
        return zap_sat, None
    if named is not None:
        return named, None
    return None, None


def zap_receipt_tags(zap: Dict[str, Any], raw: str, bolt11: str,
                     preimage: Optional[str]) -> List[List[str]]:
    """The tags of the kind-9735 receipt for a settled zap (NIP-57 Appendix E).

    ``p`` (recipient), ``P`` (sender), the forwarded ``e``/``a``/``k`` tags,
    then ``bolt11`` and ``description`` (the payer's verbatim 9734). The
    ``preimage`` tag is included when the settlement preimage is known.
    """
    tags: List[List[str]] = [["p", zap_recipient(zap) or ""], ["P", zap_sender(zap) or ""]]
    tags.extend(zap_target_tags(zap))
    tags.append(["bolt11", bolt11])
    tags.append(["description", raw])
    if preimage:
        tags.append(["preimage", preimage])
    return tags


def zap_receipt_from_target(target: ReceiptTarget) -> Optional[Dict[str, Any]]:
    """The parsed 9734 for a stored receipt target, or ``None`` when broken."""
    if not target.zap:
        return None
    return parse_zap_request(target.zap)
