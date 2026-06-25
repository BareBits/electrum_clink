"""Unit tests for the pure request-resolution policy."""

from __future__ import annotations

from clink.noffer import OfferPriceType
from clink.offers import Offer
from clink.protocol import (
    ERR_INVALID_AMOUNT,
    ERR_INVALID_OFFER,
    ERR_UNSUPPORTED_FEATURE,
    MEMO_MAX_LEN,
    IssueInvoice,
    SendError,
    invoice_message,
    receipt_payload,
    request_description,
    resolve_request,
)


def _offer(**kw) -> Offer:
    base = dict(offer_id="abc", price_type=OfferPriceType.SPONTANEOUS, active=True)
    base.update(kw)
    return Offer(**base)


def test_unknown_offer() -> None:
    res = resolve_request({"amount_sats": 1000}, None, available_sat=100_000)
    assert isinstance(res, SendError)
    assert res.payload["code"] == ERR_INVALID_OFFER


def test_inactive_offer() -> None:
    res = resolve_request({"amount_sats": 1000}, _offer(active=False), available_sat=100_000)
    assert isinstance(res, SendError) and res.payload["code"] == ERR_INVALID_OFFER


def test_spontaneous_happy_path() -> None:
    res = resolve_request({"amount_sats": 1000}, _offer(), available_sat=100_000)
    assert isinstance(res, IssueInvoice) and res.amount_sat == 1000


def test_accepts_legacy_amount_field() -> None:
    res = resolve_request({"amount": 2000}, _offer(), available_sat=100_000)
    assert isinstance(res, IssueInvoice) and res.amount_sat == 2000


def test_missing_amount_is_invalid_amount() -> None:
    res = resolve_request({}, _offer(), available_sat=100_000)
    assert isinstance(res, SendError)
    assert res.payload["code"] == ERR_INVALID_AMOUNT
    assert res.payload["range"] == {"min": 1, "max": 100_000}


def test_amount_exceeds_available_liquidity() -> None:
    res = resolve_request({"amount_sats": 150_000}, _offer(), available_sat=100_000)
    assert isinstance(res, SendError)
    assert res.payload["code"] == ERR_INVALID_AMOUNT
    assert res.payload["range"]["max"] == 100_000


def test_no_inbound_liquidity_at_all() -> None:
    res = resolve_request({"amount_sats": 1}, _offer(), available_sat=0)
    assert isinstance(res, SendError)
    assert res.payload["code"] == ERR_INVALID_AMOUNT
    assert res.payload["range"]["max"] == 0


def test_zero_or_negative_amount() -> None:
    for amt in (0, -100):
        res = resolve_request({"amount_sats": amt}, _offer(), available_sat=100_000)
        assert isinstance(res, SendError)
        assert res.payload["code"] == ERR_INVALID_AMOUNT


def test_fixed_offer_unsupported_in_v1() -> None:
    res = resolve_request({"amount_sats": 1000},
                          _offer(price_type=OfferPriceType.FIXED, price=1000),
                          available_sat=100_000)
    assert isinstance(res, SendError)
    assert res.payload["code"] == ERR_UNSUPPORTED_FEATURE


def test_exact_available_is_allowed() -> None:
    res = resolve_request({"amount_sats": 100_000}, _offer(), available_sat=100_000)
    assert isinstance(res, IssueInvoice)


def test_receipt_payload_is_sdk_shape() -> None:
    # The reference @shocknet/clink-sdk NofferReceipt type is exactly {res: 'ok'}.
    assert receipt_payload() == {"res": "ok"}


# ---- payer description extraction / invoice memo composition ----

def test_request_description_extracted_and_trimmed() -> None:
    assert request_description({"description": "  Acme Coffee  "}) == "Acme Coffee"


def test_request_description_absent_or_non_string() -> None:
    assert request_description({}) is None
    assert request_description({"description": ""}) is None
    assert request_description({"description": "   "}) is None
    assert request_description({"description": 123}) is None
    assert request_description({"description": None}) is None


def test_request_description_sanitizes_control_chars_to_single_line() -> None:
    assert request_description({"description": "Acme\nCoffee\t- 2x  Latte"}) == "Acme Coffee - 2x Latte"


def test_request_description_capped_at_memo_max() -> None:
    out = request_description({"description": "z" * 250})
    assert out == "z" * MEMO_MAX_LEN
    assert len(out) == MEMO_MAX_LEN


def test_invoice_message_combines_label_and_description() -> None:
    assert invoice_message("shop", "Acme Coffee - 2x Latte") == "shop - Acme Coffee - 2x Latte"


def test_invoice_message_drops_missing_pieces() -> None:
    assert invoice_message("shop", None) == "shop"
    assert invoice_message("", "Acme Coffee") == "Acme Coffee"
    assert invoice_message(None, "Acme Coffee") == "Acme Coffee"


def test_invoice_message_fallback_when_empty() -> None:
    assert invoice_message(None, None) == "CLINK offer"
    assert invoice_message("", "") == "CLINK offer"


def test_invoice_message_capped_at_memo_max() -> None:
    msg = invoice_message("L" * 80, "D" * 80)
    assert len(msg) <= MEMO_MAX_LEN
