"""Unit tests for the offer store (pure, dict-backed storage)."""

from __future__ import annotations

from clink.noffer import OfferPriceType
from clink.offers import Offer, OfferStore


def test_create_and_get() -> None:
    storage: dict = {}
    store = OfferStore(storage, now_fn=lambda: 42)
    offer = store.create(label="coffee")
    assert offer.label == "coffee"
    assert offer.price_type == OfferPriceType.SPONTANEOUS
    assert offer.created_at == 42
    assert store.get(offer.offer_id) == offer


def test_persistence_round_trip() -> None:
    storage: dict = {}
    store = OfferStore(storage)
    a = store.create(label="a")
    b = store.create(label="b")
    # a fresh store over the same storage sees both offers
    reloaded = OfferStore(storage)
    assert {o.offer_id for o in reloaded.list()} == {a.offer_id, b.offer_id}


def test_remove() -> None:
    storage: dict = {}
    store = OfferStore(storage)
    o = store.create()
    assert store.remove(o.offer_id)
    assert store.get(o.offer_id) is None
    assert not store.remove("nonexistent")


def test_set_active() -> None:
    storage: dict = {}
    store = OfferStore(storage)
    o = store.create()
    assert store.set_active(o.offer_id, False)
    assert OfferStore(storage).get(o.offer_id).active is False


def test_offer_ids_are_unique() -> None:
    store = OfferStore({})
    ids = {store.create().offer_id for _ in range(50)}
    assert len(ids) == 50


def test_from_dict_defaults() -> None:
    # forward-compat: a minimal stored dict still loads
    o = Offer.from_dict({"offer_id": "abc"})
    assert o.offer_id == "abc"
    assert o.price_type == OfferPriceType.SPONTANEOUS
    assert o.active is True
