"""CLINK offer request/response payloads and the pure request-resolution logic.

The wire payloads (NIP-44-decrypted JSON) and the decision of *what* to reply are
kept here, free of any relay or Electrum I/O, so the core policy — offer lookup,
spontaneous-amount handling and the inbound-liquidity gate — is unit-testable.

Request  (payer -> us):  {"offer", "amount_sats"?, "zap"?, "payer_data"?, ...}
Success  (us -> payer):  {"bolt11": "..."}
Error    (us -> payer):  {"code": int, "error": str, "range"?: {"min","max"}}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

from .noffer import OfferPriceType
from .offers import Offer

# Error codes (NIP-69).
ERR_INVALID_OFFER = 1
ERR_TEMPORARY_FAILURE = 2
ERR_EXPIRED_OFFER = 3
ERR_UNSUPPORTED_FEATURE = 4
ERR_INVALID_AMOUNT = 5


def error_payload(code: int, message: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"code": code, "error": message}
    payload.update(extra)
    return payload


def invalid_amount_payload(min_sat: int, max_sat: int) -> Dict[str, Any]:
    return error_payload(
        ERR_INVALID_AMOUNT, "Invalid Amount",
        range={"min": min_sat, "max": max_sat},
    )


def success_payload(bolt11: str) -> Dict[str, Any]:
    return {"bolt11": bolt11}


def receipt_payload(preimage: Optional[str] = None) -> Dict[str, Any]:
    """The post-payment receipt body the payer's ``onReceipt`` callback expects.

    Sent as a *second* kind-21001 event (after the invoice) once the invoice we
    issued for an offer is actually paid. Per the CLINK offers spec a standard
    Lightning payment MUST include the ``preimage`` (64-char hex) proving the
    payment settled; its absence means an internal settlement and the payload is
    the bare ``{"res": "ok"}`` the reference ``@shocknet/clink-sdk``
    ``NofferReceipt`` type is.
    """
    payload: Dict[str, Any] = {"res": "ok"}
    if preimage:
        payload["preimage"] = preimage
    return payload


# Cap for the human-readable invoice memo. The CLINK request ``description`` is
# spec-capped at 100 chars (the reference @shocknet/clink-sdk rejects longer);
# we keep the *combined* memo within the same budget so the bolt11 stays tidy.
MEMO_MAX_LEN = 100

# Bounds for decrypted (payer-controlled) request fields. Our own offer ids are
# 16 hex chars; 64 leaves headroom for other issuers' id schemes while refusing
# the pathological multi-kilobyte strings a hostile payer could otherwise push
# into logs, storage and the GUI.
MAX_OFFER_ID_LEN = 64
# Total bitcoin supply in sats — no honest request can name more.
MAX_AMOUNT_SAT = 21_000_000 * 100_000_000
# Raw ``description`` budget *before* sanitizing. Cleaning only ever shrinks
# text, so a few times the memo cap is always enough to fill it; slicing first
# bounds the work done on attacker-sized input.
_RAW_DESCRIPTION_MAX_LEN = MEMO_MAX_LEN * 4


def request_offer_id(req: Dict[str, Any]) -> Optional[str]:
    """Extract the request's offer id, validated.

    Returns the id only when it is a non-empty string of sane length; ``None``
    for a missing, mistyped (e.g. list/dict — which must never reach a dict
    lookup) or oversized value. The caller treats ``None`` as "unknown offer".
    """
    raw = req.get("offer")
    if not isinstance(raw, str) or not raw or len(raw) > MAX_OFFER_ID_LEN:
        return None
    return raw


def quantize_available_sat(available_sat: int, *, sig_figs: int = 2) -> int:
    """Round receivable capacity *down* to ``sig_figs`` significant figures.

    The invalid-amount error must advertise a range (NIP-69), but reporting the
    exact receivable capacity lets anyone holding the public noffer track the
    wallet's balance changes over time. Rounding down never over-promises: the
    gate itself keeps using the exact value, so any amount at or below the
    advertised max is always accepted.
    """
    if available_sat <= 0:
        return 0
    digits = len(str(available_sat))
    if digits <= sig_figs:
        return available_sat
    factor = 10 ** (digits - sig_figs)
    return (available_sat // factor) * factor


def request_description(req: Dict[str, Any]) -> Optional[str]:
    """Extract the payer's optional note from a request, sanitized.

    NIP-69 / the reference SDK carry an optional ``description`` (max 100 chars).
    We collapse control characters and runs of whitespace to single spaces so
    the bolt11 memo stays a clean single line, then cap the length. Returns
    ``None`` when the field is absent, not a string, or empty after cleaning.
    """
    raw = req.get("description")
    if not isinstance(raw, str):
        return None
    # Bound the work first: the plaintext can be tens of KB, the memo never is.
    raw = raw[:_RAW_DESCRIPTION_MAX_LEN]
    # Replace anything non-printable (newlines, control chars) with a space,
    # then collapse whitespace runs.
    cleaned = " ".join(
        "".join(ch if ch.isprintable() else " " for ch in raw).split()
    )
    if not cleaned:
        return None
    return cleaned[:MEMO_MAX_LEN].rstrip()


def effective_description(offer: Optional[Offer], req: Dict[str, Any]) -> Optional[str]:
    """The payer memo to fold into the invoice, honoring the offer's gate.

    Returns the sanitized payer ``description`` only when the offer exists and
    permits payer-selected memos (``allow_payer_memo``); otherwise ``None``, so
    the invoice falls back to just the merchant's label.
    """
    if offer is None or not getattr(offer, "allow_payer_memo", True):
        return None
    return request_description(req)


def invoice_message(offer_label: Optional[str], description: Optional[str]) -> str:
    """Build the bolt11 memo for an issued invoice.

    Combines the merchant's offer label with the payer's requested description
    as ``"<label> - <description>"``, dropping whichever is missing and capping
    the result at :data:`MEMO_MAX_LEN`. Falls back to a generic label when
    neither is present, preserving the prior behaviour for label-less offers.
    """
    parts = [p.strip() for p in (offer_label, description) if p and p.strip()]
    message = " - ".join(parts)
    if len(message) > MEMO_MAX_LEN:
        message = message[:MEMO_MAX_LEN].rstrip()
    return message or "CLINK offer"


def request_amount_sat(req: Dict[str, Any]) -> Optional[int]:
    """Extract the payer's requested amount, tolerating both field spellings.

    The reference SDK sends ``amount_sats``; the original NIP-69 draft used
    ``amount``. Accept either, preferring the SDK field. Strict about types:
    an integer, or a string of digits (legacy payers) — never a bool (a JSON
    ``true`` must not become a 1-sat request), float, or anything outside
    ``(0, MAX_AMOUNT_SAT]``. Returns ``None`` for anything else, which the
    resolver answers with an invalid-amount error.
    """
    raw = req.get("amount_sats", req.get("amount"))
    if isinstance(raw, bool):  # bool is an int subclass; reject it explicitly
        return None
    if isinstance(raw, str):
        # ASCII-only: isdigit() alone also accepts unicode digits (e.g. "²")
        # that int() then raises on.
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 16:
            return None
        raw = int(raw)
    if not isinstance(raw, int):
        return None
    if not (0 < raw <= MAX_AMOUNT_SAT):
        return None
    return raw


@dataclass
class IssueInvoice:
    """Resolution: mint a BOLT-11 for this many sats and reserve the liquidity."""
    amount_sat: int


@dataclass
class SendError:
    """Resolution: reply with this error payload, issue nothing."""
    payload: Dict[str, Any]


Resolution = Union[IssueInvoice, SendError]


def resolve_request(
    req: Dict[str, Any],
    offer: Optional[Offer],
    available_sat: int,
    *,
    min_sat: int = 1,
) -> Resolution:
    """Decide how to answer a decrypted offer request.

    ``available_sat`` is receivable capacity *after* existing reservations, so
    the amount check here is also the inbound-liquidity lock gate.
    """
    if offer is None or not offer.active:
        return SendError(error_payload(ERR_INVALID_OFFER, "Unknown or inactive offer"))

    if offer.price_type != OfferPriceType.SPONTANEOUS:
        # FIXED/VARIABLE are intentionally stubbed for v1.
        return SendError(error_payload(
            ERR_UNSUPPORTED_FEATURE, "Only spontaneous offers are supported"))

    amount = request_amount_sat(req)
    if amount is None or amount < min_sat:
        # Spontaneous offers require the payer to name a positive amount. The
        # advertised max is quantized so the error is not an exact-capacity oracle.
        return SendError(invalid_amount_payload(min_sat, quantize_available_sat(available_sat)))

    if amount > available_sat:
        # Not enough inbound liquidity (or it is all reserved) -> no invoice.
        return SendError(invalid_amount_payload(min_sat, quantize_available_sat(available_sat)))

    return IssueInvoice(amount)
