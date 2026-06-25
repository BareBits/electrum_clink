"""A minimal CLINK *payer* (client) used by the E2E test.

This is the mirror of the plugin's service side: decode a noffer, send a
kind-21001 request encrypted to the receiver, and wait for the kind-21001
response carrying the BOLT-11 invoice (or an error payload). It reuses the same
nostr stack and crypto modules as the plugin, so it doubles as a reference
client for the protocol.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Dict, Optional

import electrum_aionostr as aionostr
from electrum_aionostr.key import PrivateKey

from clink import nip44
from clink.noffer import noffer_decode

CLINK_EVENT_KIND = 21001


async def request_invoice(
    noffer_str: str,
    amount_sats: Optional[int],
    *,
    timeout: float = 25.0,
) -> Dict[str, Any]:
    """Send an offer request for ``noffer_str`` and return the decrypted reply.

    Returns the response payload dict: ``{"bolt11": ...}`` on success or
    ``{"code", "error", ...}`` on a protocol error.
    """
    offer = noffer_decode(noffer_str)
    sk = PrivateKey()
    my_pubkey = sk.public_key.hex()

    payload: Dict[str, Any] = {"offer": offer.offer}
    if amount_sats is not None:
        payload["amount_sats"] = amount_sats
    content = nip44.encrypt_to(sk.raw_secret, offer.pubkey, json.dumps(payload))

    manager = aionostr.Manager(relays=[offer.relay], private_key=sk.hex())
    await manager.connect()
    try:
        request_id = await aionostr._add_event(
            manager,
            kind=CLINK_EVENT_KIND,
            tags=[["p", offer.pubkey], ["clink_version", "1"]],
            content=content,
            private_key=sk.hex(),
        )
        query = {
            "kinds": [CLINK_EVENT_KIND],
            "#p": [my_pubkey],
            "#e": [request_id],
            "since": int(time.time()) - 5,
        }

        async def _await_response() -> Dict[str, Any]:
            async for event in manager.get_events(query, single_event=True, only_stored=False):
                plaintext = nip44.decrypt_from(sk.raw_secret, offer.pubkey, event.content)
                return json.loads(plaintext)
            raise RuntimeError("subscription closed before a response arrived")

        return await asyncio.wait_for(_await_response(), timeout)
    finally:
        await manager.close()


async def request_invoice_and_receipt(
    noffer_str: str,
    amount_sats: Optional[int],
    *,
    pay: Callable[[str], Any],
    timeout: float = 90.0,
) -> Dict[str, Any]:
    """Drive the full happy path *including* the post-payment receipt.

    Mirrors the reference ``@shocknet/clink-sdk``: keep the same ``#p``+``#e``
    subscription open after the invoice arrives, pay it (via the injected ``pay``
    callback, run off the event loop), then wait for the *second* kind-21001
    event — the ``{"res": "ok"}`` receipt the SDK surfaces to ``onReceipt``.

    Returns ``{"invoice": <payload>, "receipt": <payload or None>}``. ``receipt``
    is ``None`` if the first reply was an error rather than an invoice.
    """
    offer = noffer_decode(noffer_str)
    sk = PrivateKey()
    my_pubkey = sk.public_key.hex()

    payload: Dict[str, Any] = {"offer": offer.offer}
    if amount_sats is not None:
        payload["amount_sats"] = amount_sats
    content = nip44.encrypt_to(sk.raw_secret, offer.pubkey, json.dumps(payload))

    manager = aionostr.Manager(relays=[offer.relay], private_key=sk.hex())
    await manager.connect()
    try:
        request_id = await aionostr._add_event(
            manager,
            kind=CLINK_EVENT_KIND,
            tags=[["p", offer.pubkey], ["clink_version", "1"]],
            content=content,
            private_key=sk.hex(),
        )
        query = {
            "kinds": [CLINK_EVENT_KIND],
            "#p": [my_pubkey],
            "#e": [request_id],
            "since": int(time.time()) - 5,
        }

        async def _collect() -> Dict[str, Any]:
            invoice: Optional[Dict[str, Any]] = None
            async for event in manager.get_events(query, single_event=False, only_stored=False):
                msg = json.loads(nip44.decrypt_from(sk.raw_secret, offer.pubkey, event.content))
                if invoice is None:
                    invoice = msg
                    if "bolt11" not in msg:
                        return {"invoice": msg, "receipt": None}  # error, no receipt
                    # Pay off the event loop so we keep reading the relay socket.
                    await asyncio.get_running_loop().run_in_executor(None, pay, msg["bolt11"])
                else:
                    return {"invoice": invoice, "receipt": msg}
            raise RuntimeError("subscription closed before the receipt arrived")

        return await asyncio.wait_for(_collect(), timeout)
    finally:
        await manager.close()
