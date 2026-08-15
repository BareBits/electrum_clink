"""Unit tests for kind-0 metadata advertising (``clink_offer`` field).

Covers the pure policy (default-offer pick, content merge) and the server-side
reconcile loop (fetch -> merge -> publish-if-changed) against a fake manager —
no network.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from clink.clink_plugin import ClinkServer
from clink.metadata import default_offer, merge_clink_offer
from clink.noffer import OfferPriceType
from clink.offers import Offer, OfferStore

RELAY = "wss://relay.example"

# --- default-offer pick ------------------------------------------------------

def _offer(**kw) -> Offer:
    base = dict(offer_id="abc", price_type=OfferPriceType.SPONTANEOUS, active=True)
    base.update(kw)
    return Offer(**base)


def test_default_offer_none_without_offers() -> None:
    assert default_offer([]) is None


def test_default_offer_prefers_oldest_active_spontaneous() -> None:
    fixed = _offer(offer_id="f", price_type=OfferPriceType.FIXED, price=100)
    spo_a = _offer(offer_id="a")
    spo_b = _offer(offer_id="b")
    assert default_offer([fixed, spo_a, spo_b]).offer_id == "a"


def test_default_offer_falls_back_to_oldest_active_any_type() -> None:
    fixed = _offer(offer_id="f", price_type=OfferPriceType.FIXED, price=100)
    fixed2 = _offer(offer_id="g", price_type=OfferPriceType.FIXED, price=200)
    assert default_offer([fixed, fixed2]).offer_id == "f"


def test_default_offer_skips_inactive() -> None:
    active = _offer(offer_id="a")
    assert default_offer([_offer(offer_id="off", active=False), active]).offer_id == "a"


def test_default_offer_skips_expired() -> None:
    live = _offer(offer_id="a")
    assert default_offer([_offer(offer_id="dead", expires_at=1000), live], now=1001) \
        == live


def test_default_offer_skips_replaced() -> None:
    replacement = _offer(offer_id="new")
    moved = _offer(offer_id="old", active=False, replaced_by="new")
    assert default_offer([moved, replacement]).offer_id == "new"


# --- content merge -----------------------------------------------------------

def test_merge_adds_field_and_preserves_profile() -> None:
    existing = '{"name": "bob", "nip05": "bob@example.com"}'
    merged = merge_clink_offer(existing, "noffer1q...abc")
    assert json.loads(merged) == {
        "name": "bob", "nip05": "bob@example.com", "clink_offer": "noffer1q...abc"}


def test_merge_without_existing_content() -> None:
    assert json.loads(merge_clink_offer(None, "noffer1q...abc")) == \
        {"clink_offer": "noffer1q...abc"}


def test_merge_removes_field_when_no_default() -> None:
    existing = '{"name": "bob", "clink_offer": "noffer1q...old"}'
    assert json.loads(merge_clink_offer(existing, None)) == {"name": "bob"}


def test_merge_returns_raw_string_when_unchanged() -> None:
    # Formatting differences (spaces, key order) must not trigger a republish.
    raw = '{"name": "bob", "clink_offer": "noffer1q...abc"}'
    assert merge_clink_offer(raw, "noffer1q...abc") is raw
    assert merge_clink_offer(raw, None) is not raw
    assert merge_clink_offer('{"name": "bob"}', None) is not None


def test_merge_noop_removal_when_field_absent() -> None:
    raw = '{"name": "bob"}'
    assert merge_clink_offer(raw, None) is raw


def test_merge_treats_malformed_content_as_empty() -> None:
    merged = merge_clink_offer("not json at all", "noffer1q...abc")
    assert json.loads(merged) == {"clink_offer": "noffer1q...abc"}
    # No field to remove -> the (malformed) raw content is left untouched.
    assert merge_clink_offer("not json at all", None) == "not json at all"


# --- server reconcile loop ---------------------------------------------------

class _FakeManager:
    """Minimal stand-in: returns a fixed set of stored events for the fetch."""

    def __init__(self, events: List[Any]) -> None:
        self._events = events

    def get_events(self, *filters: Dict[str, Any], only_stored: bool = True,
                   single_event: bool = False, filter_future_events_sec: Optional[int] = 3600):
        async def _gen():
            for ev in self._events:
                yield ev
        return _gen()


def _server(offers: Optional[OfferStore] = None, *,
            events: Optional[List[Any]] = None,
            advertise: bool = True) -> ClinkServer:
    from electrum.logging import Logger
    from electrum_aionostr.key import PrivateKey

    server = ClinkServer.__new__(ClinkServer)
    Logger.__init__(server)
    sk = PrivateKey()
    server.private_key = sk
    server.pubkey_hex = sk.public_key.hex()
    server.config = SimpleNamespace(CLINK_ADVERTISE_METADATA=advertise)
    server.offers = offers if offers is not None else OfferStore({})
    server.offer_relay = lambda offer: RELAY  # type: ignore[method-assign]
    stored = list(events or [])
    server.manager = _FakeManager(stored)
    server.published: List[str] = []

    def remember(content: str) -> None:
        # A real relay would store the event so the next fetch sees it.
        stored.append(_metadata_event(content, created_at=1000 + len(stored)))
        server.published.append(content)

    async def publish(content: str) -> None:
        remember(content)

    server._publish_metadata_content = publish  # type: ignore[method-assign]
    return server


def _metadata_event(content: str, created_at: int = 1000) -> Any:
    return SimpleNamespace(content=content, created_at=created_at)


def _run(coro) -> Any:
    return asyncio.run(coro)


def test_sync_publishes_default_noffer_when_no_existing_metadata() -> None:
    server = _server()
    offer = server.offers.create(label="coffee")
    assert _run(server.sync_metadata()) is True
    assert len(server.published) == 1
    advertised = json.loads(server.published[0])
    assert advertised["clink_offer"].startswith("noffer1q")
    from clink.noffer import noffer_decode
    assert noffer_decode(advertised["clink_offer"]).offer == offer.offer_id


def test_sync_preserves_existing_profile_fields() -> None:
    server = _server(events=[_metadata_event('{"name": "bob", "nip05": "b@x"}')])
    server.offers.create()
    assert _run(server.sync_metadata()) is True
    assert json.loads(server.published[0])["name"] == "bob"
    assert "clink_offer" in json.loads(server.published[0])


def test_sync_idempotent_on_second_run() -> None:
    server = _server()
    server.offers.create()
    assert _run(server.sync_metadata()) is True
    assert _run(server.sync_metadata()) is False  # nothing changed -> no republish
    assert len(server.published) == 1


def test_sync_does_not_republish_when_already_advertised() -> None:
    server = _server()
    offer = server.offers.create(label="coffee")
    noffer = server.make_noffer(offer.offer_id)

    async def fake_fetch() -> str:
        return json.dumps({"name": "bob", "clink_offer": noffer})
    server._fetch_metadata_content = fake_fetch  # type: ignore[method-assign]

    assert _run(server.sync_metadata()) is False
    assert server.published == []


def test_sync_removes_field_when_default_offer_is_gone() -> None:
    server = _server(
        events=[_metadata_event(
            '{"name": "bob", "clink_offer": "noffer1qold"}')])
    # No offers left -> the advertisement must be dropped, other fields kept.
    assert _run(server.sync_metadata()) is True
    assert json.loads(server.published[0]) == {"name": "bob"}


def test_sync_noop_without_offers_and_without_metadata() -> None:
    server = _server()
    assert _run(server.sync_metadata()) is False
    assert server.published == []


def test_sync_skips_retired_default_offer() -> None:
    server = _server()
    new = server.offers.create(label="new")
    old = server.offers.create(label="old")
    server.offers.replace_with(old.offer_id, new.offer_id)
    server.offers.set_active(old.offer_id, False)
    assert _run(server.sync_metadata()) is True
    from clink.noffer import noffer_decode
    advertised = json.loads(server.published[0])["clink_offer"]
    assert noffer_decode(advertised).offer == new.offer_id


def test_sync_disabled_when_advertising_off() -> None:
    server = _server(advertise=False)
    server.offers.create()
    assert _run(server.sync_metadata()) is False
    assert server.published == []


def test_sync_noop_when_manager_down() -> None:
    server = _server()
    server.offers.create()
    server.manager = None
    assert _run(server.sync_metadata()) is False


def test_newest_metadata_event_wins() -> None:
    server = _server(events=[
        _metadata_event('{"name": "old", "clink_offer": "noffer1qold"}', created_at=100),
        _metadata_event('{"name": "new"}', created_at=200),
    ])
    server.offers.create()
    assert _run(server.sync_metadata()) is True
    advertised = json.loads(server.published[0])
    assert advertised["name"] == "new"
    assert advertised["clink_offer"].startswith("noffer1q")


def test_fetch_metadata_content_picks_newest() -> None:
    server = _server(events=[
        _metadata_event("first", created_at=100),
        _metadata_event("second", created_at=200),
        _metadata_event("third", created_at=150),
    ])
    assert _run(server._fetch_metadata_content()) == "second"


# --- plugin surface (metadata_status / advertise) ----------------------------

def _plugin(server: ClinkServer):
    from clink.clink_plugin import ClinkPlugin

    plugin = ClinkPlugin.__new__(ClinkPlugin)
    plugin.server = server
    plugin.config = SimpleNamespace(CLINK_ADVERTISE_METADATA=True)
    return plugin


def test_metadata_status_reports_default_offer() -> None:
    from clink.noffer import noffer_decode

    server = _server()
    offer = server.offers.create(label="coffee")
    status = _plugin(server).metadata_status()
    assert status["enabled"] is True
    assert status["offer_id"] == offer.offer_id
    assert noffer_decode(status["noffer"]).offer == offer.offer_id


def test_metadata_status_none_without_offers() -> None:
    status = _plugin(_server()).metadata_status()
    assert status["enabled"] is True
    assert status["offer_id"] is None and status["noffer"] is None


def test_advertise_runs_sync_and_reports_publish() -> None:
    server = _server()
    server.offers.create(label="coffee")
    plugin = _plugin(server)
    first = _run(plugin.advertise())
    assert first["published"] is True
    assert first["noffer"].startswith("noffer1q")
    second = _run(plugin.advertise())
    assert second["published"] is False  # already correct on the (fake) relay
