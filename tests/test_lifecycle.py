"""Unit tests for the plugin's wallet lifecycle (start/close/reopen).

Multi-wallet model: the plugin runs one ``ClinkServer`` per opted-in lightning
wallet, keyed by wallet file. Regression coverage is kept for the historical
field bug where closing *any* wallet — another wallet's window, or a
close-to-tray/reopen cycle — killed the CLINK listener for the rest of the
session, plus new coverage for serving several wallets at once and for the
per-wallet opt-in (enabled) flag.

Uses real ``ClinkServer`` construction over fake wallets (so the whole
start/stop surface is exercised), with the asyncio scheduling hop
(``_run_async``) captured instead of run — no event loop or network needed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from clink.clink_plugin import ClinkPlugin, ClinkServer


class FakeConfig:
    """Just the config attributes the server/settings touch in these tests."""

    CLINK_WALLET_DEFAULT_ENABLED = True  # most tests want wallets opted in
    CLINK_RELAY = ""
    CLINK_INVOICE_EXPIRY = 120
    CLINK_DEVFEE_ENABLED = False
    CLINK_DEVFEE_RATE_PERCENT = 0.1
    CLINK_DEVFEE_DEST = ""
    CLINK_DEVFEE_NOTICE_SHOWN = False
    CLINK_WS_HEARTBEAT_SEC = 30
    CLINK_WATCHDOG_INTERVAL_SEC = 60
    CLINK_LISTENER_PING_INTERVAL_SEC = 300

    def get_nostr_relays(self) -> List[str]:
        return ["wss://relay.example.com"]


def _wallet(path: str, *, lightning: bool = True, node_priv: bytes = b"\x01" * 32,
            plugin_storage: Optional[Dict[str, Any]] = None) -> Any:
    """A fake wallet. ``plugin_storage`` is the root mapping behind
    ``db.get_plugin_storage()`` — pass the same dict to two wallet objects to
    model a close/reopen of one wallet file (persisted settings/offers)."""
    storage_root: Dict[str, Any] = plugin_storage if plugin_storage is not None else {}
    return SimpleNamespace(
        has_lightning=lambda: lightning,
        lnworker=SimpleNamespace(
            node_keypair=SimpleNamespace(privkey=node_priv), network=None),
        storage=SimpleNamespace(path=path),
        network=None,
        db=SimpleNamespace(get_plugin_storage=lambda: storage_root),
    )


def _plugin(config: Optional[FakeConfig] = None) -> ClinkPlugin:
    plugin = ClinkPlugin.__new__(ClinkPlugin)
    # BasePlugin.__init__ without the global hook registration (irrelevant here)
    from electrum.logging import Logger
    Logger.__init__(plugin)
    plugin.name = "clink"
    plugin.config = config or FakeConfig()
    plugin._servers = {}
    plugin._taskgroups = {}
    # Capture scheduled coroutines instead of needing a running Electrum loop.
    plugin.scheduled: List[Any] = []

    def _run_async(coro: Any) -> None:
        plugin.scheduled.append(coro)
        # Close the scheduled coroutine and anything it holds (e.g. the
        # server.run() coroutine inside taskgroup.spawn) to avoid
        # "never awaited" warnings.
        import asyncio
        frame = getattr(coro, "cr_frame", None)
        if frame is not None:
            for value in list(frame.f_locals.values()):
                if asyncio.iscoroutine(value):
                    value.close()
        coro.close()

    plugin._run_async = _run_async  # type: ignore[method-assign]
    return plugin


@pytest.fixture()
def plugin() -> ClinkPlugin:
    p = _plugin()
    yield p
    # Never leak global event-listener callbacks between tests.
    for server in p.servers():
        server.unregister_callbacks()


def _server(plugin: ClinkPlugin, wallet: Any) -> Optional[ClinkServer]:
    return plugin._server_for(wallet)


def test_start_creates_server_and_schedules_run(plugin: ClinkPlugin) -> None:
    wallet = _wallet("/w/a")
    plugin.start_plugin(wallet)
    server = _server(plugin, wallet)
    assert isinstance(server, ClinkServer)
    assert server.wallet is wallet
    assert not server.do_stop
    assert len(plugin.scheduled) == 1


def test_start_without_lightning_is_refused(plugin: ClinkPlugin) -> None:
    wallet = _wallet("/w/a", lightning=False)
    plugin.start_plugin(wallet)
    assert _server(plugin, wallet) is None


def test_start_twice_same_wallet_is_idempotent(plugin: ClinkPlugin) -> None:
    wallet = _wallet("/w/a")
    plugin.start_plugin(wallet)
    first = _server(plugin, wallet)
    plugin.start_plugin(wallet)
    assert _server(plugin, wallet) is first
    assert len(plugin.scheduled) == 1


# --- multiple wallets at once ------------------------------------------------

def test_two_wallet_files_run_two_servers(plugin: ClinkPlugin) -> None:
    """The multi-wallet feature itself: each opted-in lightning wallet gets
    its own server with its own (node-key-derived) Nostr identity."""
    wallet_a = _wallet("/w/a")
    wallet_b = _wallet("/w/b", node_priv=b"\x02" * 32)
    plugin.start_plugin(wallet_a)
    plugin.start_plugin(wallet_b)
    server_a = _server(plugin, wallet_a)
    server_b = _server(plugin, wallet_b)
    assert server_a is not None and server_b is not None
    assert server_a is not server_b
    assert server_a.wallet is wallet_a and server_b.wallet is wallet_b
    assert server_a.pubkey_hex != server_b.pubkey_hex
    assert len(plugin.scheduled) == 2


def test_closing_one_wallet_leaves_the_other_running(plugin: ClinkPlugin) -> None:
    """THE field bug, multi-wallet edition: closing one wallet must stop only
    that wallet's server."""
    wallet_a = _wallet("/w/a")
    wallet_b = _wallet("/w/b", node_priv=b"\x02" * 32)
    plugin.start_plugin(wallet_a)
    plugin.start_plugin(wallet_b)
    server_a = _server(plugin, wallet_a)
    server_b = _server(plugin, wallet_b)

    plugin.close_wallet(wallet_b)
    assert _server(plugin, wallet_b) is None
    assert server_b.do_stop
    assert _server(plugin, wallet_a) is server_a
    assert not server_a.do_stop


def test_closing_unserved_wallet_leaves_servers_running(plugin: ClinkPlugin) -> None:
    wallet_a = _wallet("/w/a")
    plugin.start_plugin(wallet_a)
    server = _server(plugin, wallet_a)
    plugin.close_wallet(_wallet("/w/other", node_priv=b"\x02" * 32))
    assert _server(plugin, wallet_a) is server
    assert not server.do_stop


def test_servers_snapshot_lists_all(plugin: ClinkPlugin) -> None:
    plugin.start_plugin(_wallet("/w/a"))
    plugin.start_plugin(_wallet("/w/b", node_priv=b"\x02" * 32))
    assert len(plugin.servers()) == 2


# --- close/reopen cycles ------------------------------------------------------

def test_closing_driven_wallet_stops_and_detaches_server(plugin: ClinkPlugin) -> None:
    wallet = _wallet("/w/a")
    plugin.start_plugin(wallet)
    server = _server(plugin, wallet)
    plugin.close_wallet(wallet)
    assert _server(plugin, wallet) is None
    assert server.do_stop
    # the async teardown (manager close + taskgroup cancel) was scheduled
    assert len(plugin.scheduled) == 2


def test_close_without_wallet_arg_stops_all_servers(plugin: ClinkPlugin) -> None:
    # Defensive: a hook caller that passes no wallet still stops everything
    # (pre-fix behaviour, minus the stickiness).
    wallet_a = _wallet("/w/a")
    wallet_b = _wallet("/w/b", node_priv=b"\x02" * 32)
    plugin.start_plugin(wallet_a)
    plugin.start_plugin(wallet_b)
    plugin.close_wallet()
    assert plugin.servers() == []


def test_close_then_reopen_restarts_the_server(plugin: ClinkPlugin) -> None:
    """After a wallet closes, a reload must bring CLINK back up — the old
    sticky ``initialized`` flag blocked this until a full restart."""
    storage: Dict[str, Any] = {}
    wallet = _wallet("/w/a", plugin_storage=storage)
    plugin.start_plugin(wallet)
    old_taskgroup = plugin._taskgroups["/w/a"]
    plugin.close_wallet(wallet)

    # a fresh object over the same storage, as Electrum builds on reload
    reopened = _wallet("/w/a", plugin_storage=storage)
    plugin.start_plugin(reopened)
    server = _server(plugin, reopened)
    assert isinstance(server, ClinkServer)
    assert server.wallet is reopened
    assert not server.do_stop
    assert plugin._taskgroups["/w/a"] is not old_taskgroup  # fresh group per generation


def test_daemon_reload_without_close_hook_swaps_the_server(plugin: ClinkPlugin) -> None:
    """Daemon ``close_wallet``/``load_wallet`` never fires the close hook; the
    reload is only visible as a new wallet object for the same file. The stale
    server (bound to the dead wallet) must be replaced, not kept — and other
    wallets' servers must not be disturbed."""
    wallet = _wallet("/w/a")
    other = _wallet("/w/b", node_priv=b"\x02" * 32)
    plugin.start_plugin(wallet)
    plugin.start_plugin(other)
    stale = _server(plugin, wallet)
    other_server = _server(plugin, other)

    reloaded = _wallet("/w/a")
    plugin.start_plugin(reloaded)
    assert _server(plugin, reloaded) is not stale
    assert _server(plugin, reloaded).wallet is reloaded
    assert stale.do_stop  # the stale server was stopped, not leaked
    assert _server(plugin, other) is other_server
    assert not other_server.do_stop


def test_close_same_file_new_object_stops_stale_server(plugin: ClinkPlugin) -> None:
    """A daemon reload swaps the wallet object without firing Qt hooks; when
    the *reloaded* wallet later closes, the hook sees an object that is not
    ``server.wallet`` but IS the same wallet file. The stale server must be
    stopped — left running it is unreachable by any later hook and its run()
    task would hang the process-exit teardown forever."""
    plugin.start_plugin(_wallet("/w/a"))
    stale = plugin.servers()[0]
    plugin.close_wallet(_wallet("/w/a"))  # same file, fresh object
    assert plugin.servers() == []
    assert stale.do_stop


def test_close_same_file_unknown_path_leaves_server(plugin: ClinkPlugin) -> None:
    """A wallet whose file path cannot be read is not proven to be ours —
    stay conservative and keep the listener running."""
    wallet = _wallet("/w/a")
    plugin.start_plugin(wallet)
    server = _server(plugin, wallet)
    broken = _wallet("/w/a")
    broken.storage = None  # _wallet_file() -> None
    plugin.close_wallet(broken)
    assert _server(plugin, wallet) is server
    assert not server.do_stop


def test_on_close_stops_all_servers(plugin: ClinkPlugin) -> None:
    """Disabling the plugin (BasePlugin.close -> on_close) unregisters every
    hook first, so this is the last chance to stop the listeners; without it
    the servers would keep answering offers and hang the eventual exit."""
    plugin.start_plugin(_wallet("/w/a"))
    plugin.start_plugin(_wallet("/w/b", node_priv=b"\x02" * 32))
    servers = plugin.servers()
    plugin.on_close()
    assert plugin.servers() == []
    assert all(s.do_stop for s in servers)


def test_stop_server_close_cancels_taskgroup_despite_manager_error(
        plugin: ClinkPlugin) -> None:
    """The scheduled close() must cancel the run() taskgroup even when the
    relay-manager teardown raises — otherwise run() outlives the stop."""
    import asyncio

    wallet = _wallet("/w/a")
    plugin.start_plugin(wallet)
    server = _server(plugin, wallet)
    cancelled: List[bool] = []

    class ExplodingManager:
        async def close(self) -> None:
            raise RuntimeError("boom")

    class RecordingGroup:
        async def cancel_remaining(self) -> None:
            cancelled.append(True)

    server.manager = ExplodingManager()  # type: ignore[assignment]
    plugin._taskgroups["/w/a"] = RecordingGroup()  # type: ignore[assignment]

    # Capture the close() coroutine instead of closing it (the default test
    # harness _run_async closes captured coroutines unrun).
    captured: List[Any] = []
    plugin._run_async = captured.append  # type: ignore[method-assign]
    plugin.close_wallet(server.wallet)

    assert len(captured) == 1
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(captured[0])
    assert cancelled == [True]


def test_offers_survive_reload_via_stable_identity(plugin: ClinkPlugin) -> None:
    """The Nostr identity is derived from the LN node key, so a reloaded
    wallet answers for exactly the noffers handed out before the reload."""
    storage: Dict[str, Any] = {}
    wallet = _wallet("/w/a", plugin_storage=storage)
    plugin.start_plugin(wallet)
    pubkey_before = _server(plugin, wallet).pubkey_hex
    plugin.close_wallet(wallet)
    reopened = _wallet("/w/a", plugin_storage=storage)
    plugin.start_plugin(reopened)
    assert _server(plugin, reopened).pubkey_hex == pubkey_before


# --- per-wallet opt-in (enabled flag) ----------------------------------------

class OptInConfig(FakeConfig):
    CLINK_WALLET_DEFAULT_ENABLED = False  # the shipped default


@pytest.fixture()
def optin_plugin() -> ClinkPlugin:
    p = _plugin(OptInConfig())
    yield p
    for server in p.servers():
        server.unregister_callbacks()


def test_fresh_wallet_is_not_served_by_default(optin_plugin: ClinkPlugin) -> None:
    """CLINK is opt-in per wallet: a fresh wallet gets no server (and thus no
    relay connection) until the user enables it."""
    wallet = _wallet("/w/a")
    optin_plugin.start_plugin(wallet)
    assert optin_plugin.servers() == []


def test_enable_starts_and_disable_stops_the_server(optin_plugin: ClinkPlugin) -> None:
    wallet = _wallet("/w/a")
    status = optin_plugin.set_wallet_enabled(wallet, True)
    assert status["enabled"] is True and status["running"] is True
    server = optin_plugin._server_for(wallet)
    assert isinstance(server, ClinkServer)

    status = optin_plugin.set_wallet_enabled(wallet, False)
    assert status["enabled"] is False and status["running"] is False
    assert optin_plugin._server_for(wallet) is None
    assert server.do_stop


def test_enabled_flag_is_persisted_in_wallet_storage(optin_plugin: ClinkPlugin) -> None:
    """The flag lives in the wallet's plugin storage, so a reopened wallet
    (same storage, fresh object) comes back up without user action."""
    storage: Dict[str, Any] = {}
    wallet = _wallet("/w/a", plugin_storage=storage)
    optin_plugin.set_wallet_enabled(wallet, True)
    optin_plugin.close_wallet(wallet)
    assert optin_plugin.servers() == []

    reopened = _wallet("/w/a", plugin_storage=storage)
    optin_plugin.start_plugin(reopened)
    assert optin_plugin._server_for(reopened) is not None


def test_wallet_with_offers_is_grandfathered_in(optin_plugin: ClinkPlugin) -> None:
    """Migration: a wallet that already holds offers from a pre-opt-in install
    keeps serving them without any explicit enable."""
    storage = {"clink": {"offers": [{"offer_id": "o1", "label": "legacy"}]}}
    wallet = _wallet("/w/a", plugin_storage=storage)
    optin_plugin.start_plugin(wallet)
    server = optin_plugin._server_for(wallet)
    assert server is not None
    assert server.offers.get("o1") is not None


def test_explicit_disable_beats_grandfathering(optin_plugin: ClinkPlugin) -> None:
    """An explicit opt-out wins over the offers-present migration rule."""
    storage = {"clink": {"offers": [{"offer_id": "o1"}],
                         "settings": {"enabled": False}}}
    wallet = _wallet("/w/a", plugin_storage=storage)
    optin_plugin.start_plugin(wallet)
    assert optin_plugin.servers() == []


def test_global_default_can_opt_everyone_in(plugin: ClinkPlugin) -> None:
    """The rig flips plugins.clink.wallet_default_enabled=true so its wallets
    serve without a per-wallet step (the ``plugin`` fixture models this)."""
    wallet = _wallet("/w/a")
    plugin.start_plugin(wallet)
    assert plugin._server_for(wallet) is not None


# --- setting-change side effects ----------------------------------------------

def test_listener_setting_change_restarts_only_that_wallets_listener(
        plugin: ClinkPlugin) -> None:
    """Changing a listener-affecting setting (e.g. the wallet's relay) must
    rebuild that wallet's listener — and nobody else's — so the change takes
    effect without a wallet reload."""
    wallet_a = _wallet("/w/a")
    wallet_b = _wallet("/w/b", node_priv=b"\x02" * 32)
    plugin.start_plugin(wallet_a)
    plugin.start_plugin(wallet_b)
    restarts: List[str] = []
    for name, wallet in (("a", wallet_a), ("b", wallet_b)):
        server = plugin._server_for(wallet)
        server.restart_event_handler = (  # type: ignore[method-assign]
            lambda name=name: restarts.append(name))

    # A stale cached relay pick must not survive a relay change: new offers
    # would keep advertising the old relay for the cache's 24h lifetime.
    server_a = plugin._server_for(wallet_a)
    server_a._relay_selection = object()
    server_a._relay_selection_at = 1e12

    plugin.notify_setting_changed(wallet_a, "relay")
    assert restarts == ["a"]
    assert server_a._relay_selection is None
    assert server_a._relay_selection_at == 0.0
    plugin.notify_setting_changed(wallet_a, "invoice_expiry_sec")
    assert restarts == ["a"]  # non-listener knob: no restart


def test_notify_enabled_change_reconciles_server(optin_plugin: ClinkPlugin) -> None:
    """notify_setting_changed("enabled") is the clink_set_setting path for the
    opt-in flag: it must start/stop the server exactly like clink_enable."""
    storage: Dict[str, Any] = {}
    wallet = _wallet("/w/a", plugin_storage=storage)
    optin_plugin.wallet_settings(wallet).enabled = True
    optin_plugin.notify_setting_changed(wallet, "enabled")
    assert optin_plugin._server_for(wallet) is not None
    optin_plugin.wallet_settings(wallet).enabled = False
    optin_plugin.notify_setting_changed(wallet, "enabled")
    assert optin_plugin._server_for(wallet) is None
