"""NIP-44 v2 payload encryption.

CLINK wraps every request/response in NIP-44 (the bundled Electrum nostr stack
only implements the older NIP-04), so we vendor a compact, spec-exact
implementation here. It reuses Electrum's audited crypto primitives
(``chacha20`` + ``hmac``) and ``electrum_ecc`` for the secp256k1 ECDH, adding
only the NIP-44-specific HKDF, padding and framing.

Reference: https://github.com/nostr-protocol/nips/blob/master/44.md
The test-suite validates this module against the official paulmillr/nip44 vectors.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Tuple

import electrum_ecc as ecc
from electrum.crypto import chacha20_decrypt, chacha20_encrypt, hmac_oneshot

_SALT = b"nip44-v2"
_VERSION = 2
_MIN_PLAINTEXT = 1
_MAX_PLAINTEXT = 65535


def _hmac256(key: bytes, msg: bytes) -> bytes:
    return hmac_oneshot(key, msg, hashlib.sha256)


# --- key agreement -------------------------------------------------------

def ecdh_shared_x(privkey: bytes, peer_pubkey_xonly: str) -> bytes:
    """secp256k1 ECDH; return the 32-byte x-coordinate of the shared point.

    The peer key is an x-only (32-byte) nostr pubkey; NIP-44 lifts it with an
    even y (``0x02`` prefix), matching every other nostr implementation.
    """
    priv = ecc.ECPrivkey(privkey)
    pub = ecc.ECPubkey(bytes.fromhex("02" + peer_pubkey_xonly))
    shared_point = pub * priv.secret_scalar
    return int(shared_point.x()).to_bytes(32, "big")


def conversation_key(privkey: bytes, peer_pubkey_xonly: str) -> bytes:
    """HKDF-extract(salt="nip44-v2", ikm=ecdh_x) -> 32-byte conversation key."""
    return _hmac256(_SALT, ecdh_shared_x(privkey, peer_pubkey_xonly))


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = _hmac256(prk, block + info + bytes((counter,)))
        out += block
        counter += 1
    return out[:length]


def _message_keys(conv_key: bytes, nonce: bytes) -> Tuple[bytes, bytes, bytes]:
    if len(nonce) != 32:
        raise ValueError("nonce must be 32 bytes")
    keys = _hkdf_expand(conv_key, nonce, 76)
    return keys[0:32], keys[32:44], keys[44:76]  # chacha_key, chacha_nonce, hmac_key


# --- padding -------------------------------------------------------------

def _calc_padded_len(unpadded_len: int) -> int:
    if unpadded_len <= 32:
        return 32
    next_power = 1 << (unpadded_len - 1).bit_length()
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * (((unpadded_len - 1) // chunk) + 1)


def _pad(plaintext: bytes) -> bytes:
    unpadded_len = len(plaintext)
    if not (_MIN_PLAINTEXT <= unpadded_len <= _MAX_PLAINTEXT):
        raise ValueError("invalid plaintext length")
    prefix = unpadded_len.to_bytes(2, "big")
    suffix = bytes(_calc_padded_len(unpadded_len) - unpadded_len)
    return prefix + plaintext + suffix


def _unpad(padded: bytes) -> bytes:
    if len(padded) < 2:
        raise ValueError("invalid padding")
    unpadded_len = int.from_bytes(padded[:2], "big")
    plaintext = padded[2:2 + unpadded_len]
    if (len(plaintext) != unpadded_len
            or not (_MIN_PLAINTEXT <= unpadded_len <= _MAX_PLAINTEXT)
            or len(padded) != 2 + _calc_padded_len(unpadded_len)):
        raise ValueError("invalid padding")
    return plaintext


def _hmac_aad(key: bytes, message: bytes, aad: bytes) -> bytes:
    if len(aad) != 32:
        raise ValueError("aad must be 32 bytes")
    return _hmac256(key, aad + message)


# --- framing -------------------------------------------------------------

def encrypt(plaintext: str, conv_key: bytes, nonce: bytes | None = None) -> str:
    """Encrypt ``plaintext`` to a base64 NIP-44 v2 payload."""
    if nonce is None:
        nonce = secrets.token_bytes(32)
    chacha_key, chacha_nonce, hmac_key = _message_keys(conv_key, nonce)
    padded = _pad(plaintext.encode("utf-8"))
    ciphertext = chacha20_encrypt(key=chacha_key, nonce=chacha_nonce, data=padded)
    mac = _hmac_aad(hmac_key, ciphertext, nonce)
    return base64.b64encode(bytes((_VERSION,)) + nonce + ciphertext + mac).decode()


def decrypt(payload: str, conv_key: bytes) -> str:
    """Decrypt a base64 NIP-44 v2 payload, raising ``ValueError`` if invalid."""
    if payload.startswith("#"):
        raise ValueError("unsupported encryption version")
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception as e:
        raise ValueError("invalid base64 payload") from e
    if len(data) < 99 or data[0] != _VERSION:
        raise ValueError("invalid NIP-44 payload")
    nonce, ciphertext, mac = data[1:33], data[33:-32], data[-32:]
    chacha_key, chacha_nonce, hmac_key = _message_keys(conv_key, nonce)
    if not hmac.compare_digest(_hmac_aad(hmac_key, ciphertext, nonce), mac):
        raise ValueError("invalid MAC")
    padded = chacha20_decrypt(key=chacha_key, nonce=chacha_nonce, data=ciphertext)
    return _unpad(padded).decode("utf-8")


# --- convenience ---------------------------------------------------------

def encrypt_to(privkey: bytes, peer_pubkey_xonly: str, plaintext: str) -> str:
    return encrypt(plaintext, conversation_key(privkey, peer_pubkey_xonly))


def decrypt_from(privkey: bytes, peer_pubkey_xonly: str, payload: str) -> str:
    return decrypt(payload, conversation_key(privkey, peer_pubkey_xonly))
