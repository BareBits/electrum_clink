"""Unit tests for the pure request-resolution policy."""

from __future__ import annotations

from clink.noffer import OfferPriceType
from clink.offers import Offer
from clink.protocol import (
    ERR_INVALID_AMOUNT,
    ERR_INVALID_OFFER,
    ERR_UNSUPPORTED_FEATURE,
    MAX_AMOUNT_SAT,
    MAX_OFFER_ID_LEN,
    MEMO_MAX_LEN,
    IssueInvoice,
    SendError,
    effective_description,
    invoice_message,
    quantize_available_sat,
    receipt_payload,
    request_amount_sat,
    request_description,
    request_offer_id,
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


def test_receipt_payload_without_preimage_is_internal_settlement() -> None:
    # No preimage -> an "internal settlement" acknowledgment; this is also the
    # reference @shocknet/clink-sdk NofferReceipt shape ({res: 'ok'}).
    assert receipt_payload() == {"res": "ok"}
    assert receipt_payload(None) == {"res": "ok"}


def test_receipt_payload_with_preimage() -> None:
    preimage = "ab" * 32
    assert receipt_payload(preimage) == {"res": "ok", "preimage": preimage}


# ---- request field validation (attacker-controlled input) ----

def test_request_offer_id_accepts_sane_strings() -> None:
    assert request_offer_id({"offer": "abc123"}) == "abc123"
    assert request_offer_id({"offer": "z" * MAX_OFFER_ID_LEN}) == "z" * MAX_OFFER_ID_LEN


def test_request_offer_id_rejects_mistyped_or_oversized() -> None:
    for bad in (None, 5, 1.5, True, ["a"], {"a": 1}, b"x", "", "z" * (MAX_OFFER_ID_LEN + 1)):
        assert request_offer_id({"offer": bad}) is None
    assert request_offer_id({}) is None


def test_request_amount_accepts_int_and_digit_string() -> None:
    assert request_amount_sat({"amount_sats": 42}) == 42
    assert request_amount_sat({"amount_sats": "42"}) == 42
    assert request_amount_sat({"amount_sats": MAX_AMOUNT_SAT}) == MAX_AMOUNT_SAT


def test_request_amount_rejects_bool() -> None:
    # bool is an int subclass; a JSON `true` must not become a 1-sat request.
    assert request_amount_sat({"amount_sats": True}) is None
    assert request_amount_sat({"amount_sats": False}) is None


def test_request_amount_rejects_floats_and_garbage() -> None:
    for bad in (1.5, "1.5", "1e6", "-5", "+5", " 5", "5 ", [], {}, None,
                "9" * 17, "0x10", ""):
        assert request_amount_sat({"amount_sats": bad}) is None, bad


def test_request_amount_rejects_non_ascii_digits() -> None:
    # "²".isdigit() is True but int("²") raises; "٣" would silently parse.
    # Both must be refused without raising (attacker-controlled input).
    for bad in ("²", "٣٣", "１２３"):
        assert request_amount_sat({"amount_sats": bad}) is None, bad


def test_request_amount_rejects_out_of_range() -> None:
    assert request_amount_sat({"amount_sats": 0}) is None
    assert request_amount_sat({"amount_sats": -100}) is None
    assert request_amount_sat({"amount_sats": MAX_AMOUNT_SAT + 1}) is None
    # A huge JSON integer must be refused by range, never by raising.
    assert request_amount_sat({"amount_sats": 10 ** 100}) is None


def test_mistyped_amount_resolves_to_invalid_amount_error() -> None:
    res = resolve_request({"amount_sats": True}, _offer(), available_sat=100_000)
    assert isinstance(res, SendError) and res.payload["code"] == ERR_INVALID_AMOUNT


# ---- capacity quantization (liquidity-oracle mitigation) ----

def test_quantize_rounds_down_to_two_sig_figs() -> None:
    assert quantize_available_sat(1_234_567) == 1_200_000
    assert quantize_available_sat(999) == 990
    assert quantize_available_sat(101) == 100


def test_quantize_preserves_small_and_zero_values() -> None:
    assert quantize_available_sat(0) == 0
    assert quantize_available_sat(-5) == 0
    assert quantize_available_sat(1) == 1
    assert quantize_available_sat(99) == 99
    assert quantize_available_sat(100_000) == 100_000  # already round


def test_quantize_never_rounds_up() -> None:
    for v in (1, 99, 101, 12_345, 987_654_321):
        assert quantize_available_sat(v) <= v


def test_error_range_max_is_quantized() -> None:
    res = resolve_request({"amount_sats": 999_999_999}, _offer(), available_sat=1_234_567)
    assert isinstance(res, SendError)
    assert res.payload["range"]["max"] == 1_200_000


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


# ---- per-offer payer-memo gate ----

def test_effective_description_honored_when_allowed() -> None:
    offer = _offer(allow_payer_memo=True)
    assert effective_description(offer, {"description": "  hi there  "}) == "hi there"


def test_effective_description_suppressed_when_disallowed() -> None:
    offer = _offer(allow_payer_memo=False)
    assert effective_description(offer, {"description": "hi there"}) is None


def test_effective_description_none_when_offer_missing() -> None:
    assert effective_description(None, {"description": "hi there"}) is None


def test_effective_description_none_when_no_payer_memo() -> None:
    offer = _offer(allow_payer_memo=True)
    assert effective_description(offer, {}) is None
