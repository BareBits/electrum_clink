"""noffer: bech32/TLV encoding of CLINK static payment codes.

A ``noffer`` is a bech32 string (NIP-19 style, no 90-char limit) whose data is a
sequence of TLV (type-length-value) records describing where and how to request
a Lightning invoice over Nostr:

    TLV 0  receiver public key      32 raw bytes        (required)
    TLV 1  relay URL                utf-8               (required)
    TLV 2  offer identifier         utf-8               (required)
    TLV 3  price type               1 byte enum         (required)
    TLV 4  price in sats            4-byte big-endian   (optional)

Each TLV record is ``type(1) | length(1) | value(length)``; the single-byte
length caps any value at 255 bytes (fine for keys/relays/offer ids). The byte
order of records matches @shocknet/clink-sdk so our output is identical to it.

This module is intentionally dependency-light: it borrows only the pure-python
bech32 primitives that ship with Electrum (``electrum_aionostr.bech32``).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Dict, List

from electrum_aionostr.bech32 import (
    Encoding,
    bech32_decode,
    bech32_encode,
    convertbits,
)

# bech32 data limit used by the reference SDK; far above any real noffer.
BECH32_MAX_SIZE = 5000


class OfferPriceType(enum.IntEnum):
    """How the invoice amount for an offer is determined."""

    FIXED = 0        # amount is fixed at TLV 4 (price)
    VARIABLE = 1     # amount derived from a fiat price (needs an oracle)
    SPONTANEOUS = 2  # amount chosen by the payer in the request payload


@dataclass
class Noffer:
    """Decoded contents of a ``noffer`` string."""

    pubkey: str  # 32-byte schnorr/x-only public key, hex
    relay: str
    offer: str
    price_type: OfferPriceType = OfferPriceType.SPONTANEOUS
    price: int | None = None  # sats; only meaningful for FIXED/VARIABLE


def _encode_tlv(records: Dict[int, bytes]) -> bytes:
    """Serialise ``{type: value}`` records, highest type first.

    Highest-first matches @shocknet/clink-sdk's ``encodeTLV`` (which reverses
    insertion order 0..4), so encoding a given offer yields byte-identical output.
    """
    out = bytearray()
    for t in sorted(records, reverse=True):
        v = records[t]
        if len(v) > 255:
            raise ValueError(f"TLV {t} value too long: {len(v)} bytes")
        out += bytes((t, len(v))) + v
    return bytes(out)


def _parse_tlv(data: bytes) -> Dict[int, List[bytes]]:
    """Parse a TLV byte string into ``{type: [value, ...]}``."""
    result: Dict[int, List[bytes]] = {}
    rest = data
    while rest:
        if len(rest) < 2:
            raise ValueError("truncated TLV header")
        t, length = rest[0], rest[1]
        value = rest[2:2 + length]
        if len(value) < length:
            raise ValueError(f"not enough data to read TLV {t}")
        result.setdefault(t, []).append(value)
        rest = rest[2 + length:]
    return result


def noffer_encode(offer: Noffer) -> str:
    """Encode a :class:`Noffer` as a ``noffer1...`` bech32 string."""
    pubkey_bytes = bytes.fromhex(offer.pubkey)
    if len(pubkey_bytes) != 32:
        raise ValueError("pubkey must be 32 bytes")
    records: Dict[int, bytes] = {
        0: pubkey_bytes,
        1: offer.relay.encode("utf-8"),
        2: offer.offer.encode("utf-8"),
        3: bytes((int(offer.price_type),)),
    }
    # Match the SDK: only attach a price TLV when it is truthy (> 0).
    if offer.price:
        records[4] = int(offer.price).to_bytes(4, "big")
    data = _encode_tlv(records)
    words = convertbits(data, 8, 5, True)
    return bech32_encode("noffer", words, Encoding.BECH32)


def noffer_decode(code: str) -> Noffer:
    """Decode a ``noffer1...`` string, raising ``ValueError`` if malformed."""
    if not code.startswith("noffer1"):
        raise ValueError("not a noffer string")
    hrp, data5, spec = bech32_decode(code)
    if hrp != "noffer" or data5 is None:
        raise ValueError("invalid noffer bech32")
    raw = convertbits(data5, 5, 8, False)
    if raw is None:
        raise ValueError("invalid noffer payload")
    tlv = _parse_tlv(bytes(raw))

    if not tlv.get(0):
        raise ValueError("missing TLV 0 (pubkey) for noffer")
    if len(tlv[0][0]) != 32:
        raise ValueError("TLV 0 (pubkey) should be 32 bytes")
    if not tlv.get(1):
        raise ValueError("missing TLV 1 (relay) for noffer")
    if not tlv.get(2):
        raise ValueError("missing TLV 2 (offer) for noffer")
    if not tlv.get(3):
        raise ValueError("missing TLV 3 (price type) for noffer")

    return Noffer(
        pubkey=tlv[0][0].hex(),
        relay=tlv[1][0].decode("utf-8"),
        offer=tlv[2][0].decode("utf-8"),
        price_type=OfferPriceType(tlv[3][0][0]),
        price=int.from_bytes(tlv[4][0], "big") if tlv.get(4) else None,
    )
