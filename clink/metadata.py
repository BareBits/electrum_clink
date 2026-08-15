"""Kind-0 metadata advertising (``clink_offer`` field).

The CLINK spec lets a service advertise its default/primary offer in its NIP-01
user-metadata (kind-0) event:

    {"name": "bob", "nip05": "bob@example.com", "clink_offer": "noffer1..."}

The helpers here stay pure (no relay or Electrum I/O): pick which offer is the
default to advertise, and merge the ``clink_offer`` field into an existing
metadata content string without clobbering the user's other profile fields.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .noffer import OfferPriceType
from .offers import Offer
from .protocol import offer_expired


def default_offer(offers: List[Offer], now: Optional[int] = None) -> Optional[Offer]:
    """The offer to advertise in kind-0 metadata, or ``None``.

    Prefers the oldest active *spontaneous* offer — the spec's "typically for
    spontaneous payments" — falling back to the oldest active offer of any
    type. Inactive, expired, or replaced offers are never advertised. ``now``
    may be injected for deterministic tests.
    """
    active = [
        o for o in offers
        if o.active and not offer_expired(o, now) and not (o.replaced_by or "").strip()
    ]
    for o in active:
        if o.price_type == OfferPriceType.SPONTANEOUS:
            return o
    return active[0] if active else None


def merge_clink_offer(content: Optional[str], noffer: Optional[str]) -> str:
    """Kind-0 content with ``clink_offer`` set to ``noffer``.

    Every existing profile field is preserved; ``None`` ``noffer`` drops the
    field (nothing left to advertise). Returns the caller's own string
    unchanged when the field is already correct — so formatting differences
    never trigger a redundant republish. Malformed content is treated as empty
    rather than losing the rest of the profile.
    """
    raw = content or ""
    parsed: Dict[str, Any] = {}
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                parsed = loaded
        except (ValueError, TypeError):
            pass
    if noffer:
        if parsed.get("clink_offer") == noffer:
            return raw
        parsed["clink_offer"] = noffer
    else:
        if "clink_offer" not in parsed:
            return raw
        parsed.pop("clink_offer", None)
    return json.dumps(parsed, separators=(",", ":"), sort_keys=True)
