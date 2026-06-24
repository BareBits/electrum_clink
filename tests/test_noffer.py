"""Unit tests for noffer TLV/bech32 encoding.

Anchored on real strings produced by @shocknet/clink-sdk (tests/vectors), so a
passing suite proves byte-level interop with the reference implementation.
"""

from __future__ import annotations

import pytest

from clink.noffer import Noffer, OfferPriceType, noffer_decode, noffer_encode


def test_decode_matches_sdk_fields(noffer_vectors) -> None:
    for vec in noffer_vectors:
        decoded = noffer_decode(vec["noffer"])
        exp = vec["decoded"]
        assert decoded.pubkey == exp["pubkey"]
        assert decoded.relay == exp["relay"]
        assert decoded.offer == exp["offer"]
        assert int(decoded.price_type) == exp["priceType"]
        assert decoded.price == exp.get("price")


def test_encode_is_byte_identical_to_sdk(noffer_vectors) -> None:
    for vec in noffer_vectors:
        inp = vec["input"]
        offer = Noffer(
            pubkey=inp["pubkey"],
            relay=inp["relay"],
            offer=inp["offer"],
            price_type=OfferPriceType(inp["priceType"]),
            price=inp.get("price"),
        )
        assert noffer_encode(offer) == vec["noffer"], vec["name"]


def test_round_trip_spontaneous() -> None:
    offer = Noffer(
        pubkey="11" * 32,
        relay="ws://127.0.0.1:7777",
        offer="my-offer",
        price_type=OfferPriceType.SPONTANEOUS,
    )
    assert noffer_decode(noffer_encode(offer)) == offer


def test_price_zero_is_omitted() -> None:
    # SDK treats a falsy price as "no price TLV"; round-trip must yield None.
    offer = Noffer(pubkey="22" * 32, relay="ws://r", offer="o",
                   price_type=OfferPriceType.SPONTANEOUS, price=0)
    decoded = noffer_decode(noffer_encode(offer))
    assert decoded.price is None


def test_reject_non_noffer() -> None:
    with pytest.raises(ValueError):
        noffer_decode("nprofile1foo")


def test_reject_bad_pubkey_length() -> None:
    with pytest.raises(ValueError):
        noffer_encode(Noffer(pubkey="00", relay="ws://r", offer="o",
                             price_type=OfferPriceType.SPONTANEOUS))
