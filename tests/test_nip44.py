"""NIP-44 v2 conformance against the official paulmillr/nip44 vectors."""

from __future__ import annotations

import hashlib

import pytest

from clink import nip44


def test_get_conversation_key(nip44_vectors) -> None:
    for vec in nip44_vectors["valid"]["get_conversation_key"]:
        got = nip44.conversation_key(bytes.fromhex(vec["sec1"]), vec["pub2"])
        assert got.hex() == vec["conversation_key"]


def test_get_message_keys(nip44_vectors) -> None:
    block = nip44_vectors["valid"]["get_message_keys"]
    conv_key = bytes.fromhex(block["conversation_key"])
    for keyset in block["keys"]:
        cc_key, cc_nonce, hmac_key = nip44._message_keys(
            conv_key, bytes.fromhex(keyset["nonce"]))
        assert cc_key.hex() == keyset["chacha_key"]
        assert cc_nonce.hex() == keyset["chacha_nonce"]
        assert hmac_key.hex() == keyset["hmac_key"]


def test_calc_padded_len(nip44_vectors) -> None:
    for unpadded, padded in nip44_vectors["valid"]["calc_padded_len"]:
        assert nip44._calc_padded_len(unpadded) == padded


def test_encrypt_decrypt_roundtrip_and_payload(nip44_vectors) -> None:
    for vec in nip44_vectors["valid"]["encrypt_decrypt"]:
        conv_key = nip44.conversation_key(bytes.fromhex(vec["sec1"]), vec["pub2"]) \
            if "pub2" in vec else bytes.fromhex(vec["conversation_key"])
        nonce = bytes.fromhex(vec["nonce"])
        # forced-nonce encryption must reproduce the exact payload
        assert nip44.encrypt(vec["plaintext"], conv_key, nonce) == vec["payload"]
        # and decryption must recover the plaintext
        assert nip44.decrypt(vec["payload"], conv_key) == vec["plaintext"]


def test_encrypt_decrypt_long_msg(nip44_vectors) -> None:
    for vec in nip44_vectors["valid"]["encrypt_decrypt_long_msg"]:
        conv_key = bytes.fromhex(vec["conversation_key"])
        nonce = bytes.fromhex(vec["nonce"])
        plaintext = vec["pattern"] * vec["repeat"]
        assert hashlib.sha256(plaintext.encode()).hexdigest() == vec["plaintext_sha256"]
        payload = nip44.encrypt(plaintext, conv_key, nonce)
        assert hashlib.sha256(payload.encode()).hexdigest() == vec["payload_sha256"]
        assert nip44.decrypt(payload, conv_key) == plaintext


def test_decrypt_rejects_tampered_mac(nip44_vectors) -> None:
    vec = nip44_vectors["valid"]["encrypt_decrypt"][0]
    conv_key = bytes.fromhex(vec["conversation_key"])
    bad = vec["payload"][:-4] + ("AAAA" if not vec["payload"].endswith("AAAA") else "BBBB")
    with pytest.raises(ValueError):
        nip44.decrypt(bad, conv_key)


def test_decrypt_invalid_vectors(nip44_vectors) -> None:
    for vec in nip44_vectors["invalid"]["decrypt"]:
        conv_key = bytes.fromhex(vec["conversation_key"])
        with pytest.raises(Exception):
            nip44.decrypt(vec["payload"], conv_key)
