"""Unit tests for relay pinning: the auto-picked relay is stored on the offer.

Before this behaviour existed, the probed relay selection lived only in memory:
after a wallet reload the noffer silently re-derived its relay from the raw
config order (typically a relay that fails the payability probe — the exact
"shows a different, unreachable relay after restart" bug). These tests cover:

  * ``create_offer`` pins the probed pick on the new offer and *blocks*
    creation when no relay passes the probe (no more created-with-warning)
  * ``pick_payable_relay`` migrates pre-pinning legacy offers (empty relay)
    onto a successful pick — from the fresh-probe path and the 24h-cache path
  * ``ensure_offer_relays_pinned`` triggers that migration at startup only
    while a legacy offer still needs it

All network entry points are faked; the offer store and the pin/derive logic
under test are real.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Coroutine, Dict, List, Optional, Tuple

import pytest
from electrum.logging import Logger
from electrum.util import UserFacingException
from electrum_aionostr.key import PrivateKey

import clink.clink_plugin as clink_plugin_mod
from clink.clink_plugin import ClinkPlugin, ClinkServer
from clink.offers import OfferStore
from clink.relay_probe import ProbeResult, ProbeStatus, RelaySelection

PICKED = "wss://picked.example"
FALLBACK = "wss://first-in-config.example"


def _selection(ok: bool, relay: str = PICKED) -> RelaySelection:
    status = ProbeStatus.OK if ok else ProbeStatus.NO_READBACK
    return RelaySelection(
        relay=relay, ok=ok, results=[ProbeResult(relay=relay, status=status)])


def _server(offers: Optional[OfferStore] = None) -> Tuple[ClinkServer, List[str]]:
    """A ``ClinkServer`` shell wired with exactly what the pick/pin path uses."""
    server = ClinkServer.__new__(ClinkServer)
    Logger.__init__(server)
    server.config = SimpleNamespace(
        CLINK_RELAY="",
        get_nostr_relays=lambda: [FALLBACK, PICKED],
    )
    server.offers = offers if offers is not None else OfferStore({})
    server.ssl_context = None
    server._proxy_factory = lambda: None
    server._relay_selection = None
    server._relay_selection_at = 0.0
    server._relay_pick_lock = asyncio.Lock()
    events: List[str] = []
    server.restart_event_handler = lambda: events.append("restart")  # type: ignore[method-assign]
    return server, events


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


# --- pick_payable_relay pins legacy offers --------------------------------

def test_fresh_pick_pins_legacy_offers(monkeypatch: pytest.MonkeyPatch) -> None:
    storage: Dict[str, Any] = {}
    store = OfferStore(storage)
    legacy = store.create(label="legacy")             # pre-pinning: no relay
    custom = store.create(relay="wss://mine.example", relay_custom=True)
    server, events = _server(store)

    async def fake_select(candidates: Any, *, probe: Any) -> RelaySelection:
        return _selection(ok=True)

    monkeypatch.setattr(clink_plugin_mod, "select_payable_relay", fake_select)
    sel = _run(server.pick_payable_relay())
    assert sel.ok and sel.relay == PICKED

    reloaded = OfferStore(storage)  # migration persisted
    assert reloaded.get(legacy.offer_id).relay == PICKED
    assert reloaded.get(legacy.offer_id).relay_custom is False
    assert reloaded.get(custom.offer_id).relay == "wss://mine.example"
    assert "restart" in events  # the listener set changed


def test_cached_pick_still_pins_legacy_offers() -> None:
    # A fresh-enough cached selection returns early — legacy offers loaded
    # after it was cached (wallet reload race) must still be migrated.
    store = OfferStore({})
    legacy = store.create(label="legacy")
    server, _events = _server(store)
    server._relay_selection = _selection(ok=True)
    server._relay_selection_at = time.time()

    sel = _run(server.pick_payable_relay())
    assert sel.relay == PICKED
    assert store.get(legacy.offer_id).relay == PICKED


def test_failed_pick_pins_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    store = OfferStore({})
    legacy = store.create(label="legacy")
    server, _events = _server(store)

    async def fake_select(candidates: Any, *, probe: Any) -> RelaySelection:
        return _selection(ok=False, relay=FALLBACK)

    monkeypatch.setattr(clink_plugin_mod, "select_payable_relay", fake_select)
    sel = _run(server.pick_payable_relay())
    assert not sel.ok
    assert store.get(legacy.offer_id).relay == ""  # still dynamic, can heal later


# --- ensure_offer_relays_pinned (startup migration trigger) ----------------

def _ensure_server(store: OfferStore) -> Tuple[ClinkServer, List[str]]:
    server, _events = _server(store)
    calls: List[str] = []

    async def fake_pick(*, force: bool = False) -> RelaySelection:
        calls.append("pick")
        server.offers.pin_missing_relays(PICKED)
        return _selection(ok=True)

    server.pick_payable_relay = fake_pick  # type: ignore[method-assign]
    return server, calls


def test_startup_pins_when_a_legacy_offer_exists() -> None:
    store = OfferStore({})
    legacy = store.create(label="legacy")
    server, calls = _ensure_server(store)
    _run(server.ensure_offer_relays_pinned())
    assert calls == ["pick"]
    assert store.get(legacy.offer_id).relay == PICKED


def test_startup_skips_pick_once_everything_is_pinned() -> None:
    store = OfferStore({})
    store.create(relay=PICKED)
    server, calls = _ensure_server(store)
    _run(server.ensure_offer_relays_pinned())
    assert calls == []


def test_startup_pick_failure_is_swallowed() -> None:
    server, _events = _server(OfferStore({}))
    server.offers.create(label="legacy")

    async def broken_pick(*, force: bool = False) -> RelaySelection:
        raise RuntimeError("network down")

    server.pick_payable_relay = broken_pick  # type: ignore[method-assign]
    _run(server.ensure_offer_relays_pinned())  # must not raise


# --- create_offer pins the pick / blocks on failure ------------------------

def _plugin(selection: RelaySelection) -> Tuple[ClinkPlugin, ClinkServer, List[str]]:
    server, events = _server()
    server_sk = PrivateKey()
    server.private_key = server_sk
    server.pubkey_hex = server_sk.public_key.hex()

    async def fake_pick(*, force: bool = False) -> RelaySelection:
        server._relay_selection = selection
        server._relay_selection_at = time.time()
        return selection

    server.pick_payable_relay = fake_pick  # type: ignore[method-assign]
    plugin = ClinkPlugin.__new__(ClinkPlugin)
    plugin.server = server
    return plugin, server, events


def test_create_offer_pins_probed_relay() -> None:
    from clink.noffer import noffer_decode

    plugin, server, _events = _plugin(_selection(ok=True))
    result = _run(plugin.create_offer(label="coffee"))
    offer = server.offers.get(result["offer_id"])
    assert offer is not None
    assert offer.relay == PICKED
    assert offer.relay_custom is False
    assert result["relay"] == PICKED
    assert result["relay_payable"] is True
    assert noffer_decode(result["noffer"]).relay == PICKED


def test_create_fixed_price_offer_advertises_price_in_noffer() -> None:
    from clink.noffer import OfferPriceType, noffer_decode

    plugin, server, _events = _plugin(_selection(ok=True))
    result = _run(plugin.create_offer(label="coffee", price=25000))
    offer = server.offers.get(result["offer_id"])
    assert offer is not None
    assert offer.price_type == OfferPriceType.FIXED
    assert offer.price == 25000
    assert result["price_type"] == 0 and result["price"] == 25000
    decoded = noffer_decode(result["noffer"])
    assert decoded.price_type == OfferPriceType.FIXED
    assert decoded.price == 25000


def test_create_offer_price_validation() -> None:
    plugin, server, _events = _plugin(_selection(ok=True))
    for price in (-5, 1.5, True):
        with pytest.raises(UserFacingException, match="price"):
            _run(plugin.create_offer(label="x", price=price))
    assert server.offers.list() == []
    # 0 / omitted is still a spontaneous offer.
    for price in (0, None):
        result = _run(plugin.create_offer(label="x", price=price))
        assert result["price_type"] == 2 and result["price"] is None
    assert len(server.offers.list()) == 2


def test_create_offer_blocks_when_no_relay_is_payable() -> None:
    plugin, server, _events = _plugin(_selection(ok=False, relay=FALLBACK))
    with pytest.raises(UserFacingException, match="not created"):
        _run(plugin.create_offer(label="coffee"))
    assert server.offers.list() == []


def test_create_offer_restarts_listener_for_uncovered_relay() -> None:
    # All existing offers are pinned elsewhere, so the listener no longer sits
    # on the picked relay: creating the offer must restart it.
    plugin, server, events = _plugin(_selection(ok=True))
    server.offers.create(relay="wss://elsewhere.example", relay_custom=True)
    _run(plugin.create_offer(label="coffee"))
    assert "restart" in events


def test_pinned_relay_survives_selection_loss() -> None:
    # The regression this whole change is about: after a restart the in-memory
    # selection is gone and the fallback differs — the noffer must not change.
    plugin, server, _events = _plugin(_selection(ok=True))
    result = _run(plugin.create_offer(label="coffee"))
    server._relay_selection = None  # simulate wallet reload
    server._relay_selection_at = 0.0
    listed = plugin.list_offers()[result["offer_id"]]
    assert listed["relay"] == PICKED
    assert listed["relay_pinned"] is True
    assert listed["relay_custom"] is False
    assert listed["noffer"] == result["noffer"]
