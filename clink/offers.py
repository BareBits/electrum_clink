"""Offer model and persistence.

For v1 every offer is *spontaneous* (the payer names the amount), so an offer is
little more than a stable identifier plus a human label. The model carries a
``price_type`` and ``price`` field anyway so FIXED/VARIABLE can be filled in
later without a storage migration.

Persistence is delegated to an injected ``storage`` mapping (the plugin passes
the wallet DB's plugin storage), keeping this module unit-testable with a plain
dict.
"""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, MutableMapping, Optional

from .noffer import OfferPriceType


def _new_offer_id() -> str:
    # Short, URL/bech32-safe, collision-resistant enough for per-wallet offer ids.
    return secrets.token_hex(8)


@dataclass
class Offer:
    offer_id: str
    label: str = ""
    price_type: OfferPriceType = OfferPriceType.SPONTANEOUS
    price: Optional[int] = None  # sats; reserved for FIXED/VARIABLE
    active: bool = True
    created_at: int = 0
    # Whether a payer's requested memo (NIP-69 ``description``) is folded into the
    # issued invoice. Defaults to ``True`` so pre-existing offers keep honoring
    # memos (the behaviour before this flag existed).
    allow_payer_memo: bool = True
    # Relay URL (``wss://…``) this offer's noffer advertises, pinned at
    # creation (either the user's explicit choice or the auto-probed pick) so
    # the noffer handed to payers never changes across restarts. Empty only on
    # offers stored before pinning existed; those fall back to the plugin-wide
    # relay until :meth:`OfferStore.pin_missing_relays` migrates them.
    relay: str = ""
    # True when ``relay`` was chosen explicitly by the user rather than pinned
    # from the automatic payability probe (display-only distinction).
    relay_custom: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["price_type"] = int(self.price_type)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Offer":
        return cls(
            offer_id=d["offer_id"],
            label=d.get("label", ""),
            price_type=OfferPriceType(d.get("price_type", int(OfferPriceType.SPONTANEOUS))),
            price=d.get("price"),
            active=d.get("active", True),
            created_at=d.get("created_at", 0),
            allow_payer_memo=d.get("allow_payer_memo", True),
            relay=d.get("relay", ""),
            # Offers stored before this flag existed could only carry a
            # non-empty relay if the user chose it explicitly.
            relay_custom=bool(d.get("relay_custom", bool(d.get("relay", "")))),
        )


def advertised_relay(offer: Optional[Offer], default_relay: str) -> str:
    """The relay ``offer``'s noffer should advertise.

    An offer's own pinned relay wins; otherwise (no offer, or a pre-pinning
    legacy offer) the plugin-wide auto-selected ``default_relay``.
    """
    if offer is not None and offer.relay.strip():
        return offer.relay.strip()
    return default_relay


def advertised_relays(offers: Iterable[Offer], default_relay: str) -> List[str]:
    """Every distinct relay some existing offer's noffer advertises.

    Unlike :func:`listen_relays` this is empty when there are no offers: with
    nothing a payer could hold, there is no advertised relay to care about
    (the liveness monitor probes exactly this list).
    """
    out: List[str] = []
    for offer in offers:
        url = advertised_relay(offer, default_relay).strip()
        if url and url not in out:
            out.append(url)
    return out


def listen_relays(offers: Iterable[Offer], default_relay: str) -> List[str]:
    """Every relay the plugin must listen on so all ``offers`` stay payable.

    Each offer's pinned relay in offer order — deduped, stripped, blanks
    dropped. The plugin-wide default relay is prepended only while it is still
    needed: when there are no offers yet (so the listener is up before the
    first one is created) or while a pre-pinning legacy offer still falls back
    to it.
    """
    offer_list = list(offers)
    urls: List[str] = []
    if not offer_list or any(not (o.relay or "").strip() for o in offer_list):
        urls.append(default_relay)
    urls.extend(o.relay for o in offer_list)
    out: List[str] = []
    for url in urls:
        url = (url or "").strip()
        if url and url not in out:
            out.append(url)
    return out


class OfferStore:
    """A persisted collection of offers, keyed by ``offer_id``."""

    STORAGE_KEY = "offers"

    def __init__(self, storage: MutableMapping[str, Any], now_fn=None) -> None:
        self._storage = storage
        self._now_fn = now_fn or (lambda: 0)
        self._offers: Dict[str, Offer] = {}
        for raw in self._storage.get(self.STORAGE_KEY, []):
            offer = Offer.from_dict(raw)
            self._offers[offer.offer_id] = offer

    def _persist(self) -> None:
        self._storage[self.STORAGE_KEY] = [o.to_dict() for o in self._offers.values()]

    def create(self, label: str = "",
               price_type: OfferPriceType = OfferPriceType.SPONTANEOUS,
               price: Optional[int] = None,
               allow_payer_memo: bool = True,
               relay: str = "",
               relay_custom: bool = False) -> Offer:
        offer = Offer(
            offer_id=_new_offer_id(),
            label=label,
            price_type=price_type,
            price=price,
            created_at=int(self._now_fn()),
            allow_payer_memo=allow_payer_memo,
            relay=relay.strip(),
            relay_custom=bool(relay_custom) and bool(relay.strip()),
        )
        self._offers[offer.offer_id] = offer
        self._persist()
        return offer

    def pin_missing_relays(self, relay: str) -> List[str]:
        """Pin ``relay`` on every offer that has none; return the ids changed.

        One-time migration for offers stored before relays were pinned at
        creation: the first successful payability probe writes its pick onto
        them, so from then on their noffers survive restarts unchanged.
        """
        relay = (relay or "").strip()
        if not relay:
            return []
        pinned: List[str] = []
        for offer in self._offers.values():
            if not offer.relay.strip():
                offer.relay = relay
                offer.relay_custom = False
                pinned.append(offer.offer_id)
        if pinned:
            self._persist()
        return pinned

    def get(self, offer_id: str) -> Optional[Offer]:
        return self._offers.get(offer_id)

    def list(self) -> List[Offer]:
        return list(self._offers.values())

    def remove(self, offer_id: str) -> bool:
        if offer_id in self._offers:
            del self._offers[offer_id]
            self._persist()
            return True
        return False

    def set_active(self, offer_id: str, active: bool) -> bool:
        offer = self._offers.get(offer_id)
        if offer is None:
            return False
        offer.active = active
        self._persist()
        return True

    def set_label(self, offer_id: str, label: str) -> bool:
        offer = self._offers.get(offer_id)
        if offer is None:
            return False
        offer.label = label
        self._persist()
        return True

    def set_allow_payer_memo(self, offer_id: str, allow: bool) -> bool:
        offer = self._offers.get(offer_id)
        if offer is None:
            return False
        offer.allow_payer_memo = bool(allow)
        self._persist()
        return True
