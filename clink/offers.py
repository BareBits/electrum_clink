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
from typing import Any, Dict, List, MutableMapping, Optional

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
        )


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
               price: Optional[int] = None) -> Offer:
        offer = Offer(
            offer_id=_new_offer_id(),
            label=label,
            price_type=price_type,
            price=price,
            created_at=int(self._now_fn()),
        )
        self._offers[offer.offer_id] = offer
        self._persist()
        return offer

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
