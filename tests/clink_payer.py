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
from typing import Any, Dict, Optional

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
