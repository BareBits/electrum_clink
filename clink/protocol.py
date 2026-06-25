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


def receipt_payload() -> Dict[str, Any]:
    """The post-payment receipt body the payer's ``onReceipt`` callback expects.

    Sent as a *second* kind-21001 event (after the invoice) once the invoice we
    issued for an offer is actually paid. Kept byte-compatible with the reference
    ``@shocknet/clink-sdk`` ``NofferReceipt`` type, which is exactly ``{res: 'ok'}``.
    """
    return {"res": "ok"}


def request_amount_sat(req: Dict[str, Any]) -> Optional[int]:
    """Extract the payer's requested amount, tolerating both field spellings.

    The reference SDK sends ``amount_sats``; the original NIP-69 draft used
    ``amount``. Accept either, preferring the SDK field.
    """
    raw = req.get("amount_sats", req.get("amount"))
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


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
        # Spontaneous offers require the payer to name a positive amount.
        return SendError(invalid_amount_payload(min_sat, available_sat))

    if amount > available_sat:
        # Not enough inbound liquidity (or it is all reserved) -> no invoice.
        return SendError(invalid_amount_payload(min_sat, available_sat))

    return IssueInvoice(amount)
