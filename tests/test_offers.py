"""Unit tests for the offer store (pure, dict-backed storage)."""

from __future__ import annotations

from clink.noffer import OfferPriceType
from clink.offers import (
    Offer,
    OfferStore,
    advertised_relay,
    advertised_relays,
    listen_relays,
)


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


def test_set_label_persists() -> None:
    storage: dict = {}
    store = OfferStore(storage)
    o = store.create(label="old")
    assert store.set_label(o.offer_id, "new")
    assert OfferStore(storage).get(o.offer_id).label == "new"
    assert not store.set_label("nonexistent", "x")


def test_allow_payer_memo_default_and_create() -> None:
    store = OfferStore({})
    assert store.create().allow_payer_memo is True
    assert store.create(allow_payer_memo=False).allow_payer_memo is False


def test_set_allow_payer_memo_persists() -> None:
    storage: dict = {}
    store = OfferStore(storage)
    o = store.create()  # defaults to allowed
    assert store.set_allow_payer_memo(o.offer_id, False)
    assert OfferStore(storage).get(o.offer_id).allow_payer_memo is False
    assert store.set_allow_payer_memo(o.offer_id, True)
    assert OfferStore(storage).get(o.offer_id).allow_payer_memo is True
    assert not store.set_allow_payer_memo("nonexistent", True)


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
    # offers stored before the flag existed keep honoring payer memos
    assert o.allow_payer_memo is True


def test_allow_payer_memo_round_trips_through_dict() -> None:
    o = Offer.from_dict(Offer(offer_id="x", allow_payer_memo=False).to_dict())
    assert o.allow_payer_memo is False


# --- per-offer custom relay ----------------------------------------------

def test_relay_defaults_to_automatic() -> None:
    store = OfferStore({})
    assert store.create().relay == ""
    # offers stored before the field existed load as "automatic"
    assert Offer.from_dict({"offer_id": "abc"}).relay == ""


def test_custom_relay_persists() -> None:
    storage: dict = {}
    store = OfferStore(storage)
    o = store.create(relay="wss://myrelay.com:7777")
    assert o.relay == "wss://myrelay.com:7777"
    assert OfferStore(storage).get(o.offer_id).relay == "wss://myrelay.com:7777"


def test_create_strips_relay_whitespace() -> None:
    assert OfferStore({}).create(relay="  wss://r.example  ").relay == "wss://r.example"


def test_relay_round_trips_through_dict() -> None:
    o = Offer.from_dict(Offer(offer_id="x", relay="wss://r.example").to_dict())
    assert o.relay == "wss://r.example"


def test_advertised_relay_prefers_offer_override() -> None:
    default = "wss://auto.example"
    assert advertised_relay(None, default) == default
    assert advertised_relay(Offer(offer_id="a"), default) == default
    assert advertised_relay(Offer(offer_id="a", relay="  "), default) == default
    assert advertised_relay(
        Offer(offer_id="a", relay="wss://mine.example"), default) == "wss://mine.example"


def test_listen_relays_unions_default_and_custom() -> None:
    offers = [
        Offer(offer_id="a"),                              # automatic
        Offer(offer_id="b", relay="wss://one.example"),
        Offer(offer_id="c", relay="wss://two.example"),
        Offer(offer_id="d", relay="wss://one.example"),   # duplicate
        Offer(offer_id="e", relay="wss://auto.example"),  # same as default
    ]
    assert listen_relays(offers, "wss://auto.example") == [
        "wss://auto.example", "wss://one.example", "wss://two.example"]


def test_listen_relays_drops_blanks_and_keeps_order() -> None:
    offers = [Offer(offer_id="a", relay="  "), Offer(offer_id="b", relay="wss://x.example")]
    assert listen_relays(offers, "") == ["wss://x.example"]
    assert listen_relays([], "wss://auto.example") == ["wss://auto.example"]


def test_advertised_relays_covers_each_offers_noffer_once() -> None:
    offers = [
        Offer(offer_id="a"),                              # automatic
        Offer(offer_id="b", relay="wss://one.example"),
        Offer(offer_id="c", relay="wss://one.example"),   # duplicate
        Offer(offer_id="d", relay="wss://auto.example"),  # same as default
    ]
    assert advertised_relays(offers, "wss://auto.example") == [
        "wss://auto.example", "wss://one.example"]


def test_advertised_relays_empty_without_offers() -> None:
    # Unlike listen_relays, no offers -> nothing advertised -> nothing to probe.
    assert advertised_relays([], "wss://auto.example") == []


def test_advertised_relays_all_custom_skips_default() -> None:
    offers = [Offer(offer_id="a", relay="wss://mine.example")]
    assert advertised_relays(offers, "wss://auto.example") == ["wss://mine.example"]


# --- pinned relays (auto pick stored on the offer at creation) ------------

def test_create_pins_auto_relay_as_non_custom() -> None:
    o = OfferStore({}).create(relay="wss://picked.example", relay_custom=False)
    assert o.relay == "wss://picked.example"
    assert o.relay_custom is False


def test_create_marks_custom_relay() -> None:
    o = OfferStore({}).create(relay="wss://mine.example", relay_custom=True)
    assert o.relay_custom is True
    # the flag is meaningless without a relay and must not stick
    assert OfferStore({}).create(relay="", relay_custom=True).relay_custom is False


def test_relay_custom_round_trips_through_dict() -> None:
    for custom in (True, False):
        o = Offer.from_dict(
            Offer(offer_id="x", relay="wss://r.example", relay_custom=custom).to_dict())
        assert o.relay_custom is custom


def test_relay_custom_legacy_defaults() -> None:
    # pre-flag stored offers: a non-empty relay could only be user-chosen
    assert Offer.from_dict({"offer_id": "a"}).relay_custom is False
    assert Offer.from_dict(
        {"offer_id": "a", "relay": "wss://mine.example"}).relay_custom is True


def test_pin_missing_relays_migrates_only_unpinned_offers() -> None:
    storage: dict = {}
    store = OfferStore(storage)
    legacy_a = store.create()                                  # dynamic (legacy)
    legacy_b = store.create()                                  # dynamic (legacy)
    custom = store.create(relay="wss://mine.example", relay_custom=True)
    pinned = store.pin_missing_relays("wss://picked.example")
    assert sorted(pinned) == sorted([legacy_a.offer_id, legacy_b.offer_id])
    reloaded = OfferStore(storage)  # the migration must persist
    for oid in (legacy_a.offer_id, legacy_b.offer_id):
        assert reloaded.get(oid).relay == "wss://picked.example"
        assert reloaded.get(oid).relay_custom is False
    assert reloaded.get(custom.offer_id).relay == "wss://mine.example"
    assert reloaded.get(custom.offer_id).relay_custom is True
    # second run: nothing left to migrate
    assert store.pin_missing_relays("wss://other.example") == []


def test_pin_missing_relays_ignores_blank_relay() -> None:
    store = OfferStore({})
    o = store.create()
    assert store.pin_missing_relays("   ") == []
    assert store.get(o.offer_id).relay == ""


def test_listen_relays_skips_default_once_all_offers_pinned() -> None:
    offers = [
        Offer(offer_id="a", relay="wss://one.example"),
        Offer(offer_id="b", relay="wss://two.example"),
    ]
    # every offer carries its own relay -> no reason to sit on the default too
    assert listen_relays(offers, "wss://auto.example") == [
        "wss://one.example", "wss://two.example"]
    # ...but a still-unpinned legacy offer brings the default back
    offers.append(Offer(offer_id="c"))
    assert listen_relays(offers, "wss://auto.example") == [
        "wss://auto.example", "wss://one.example", "wss://two.example"]
