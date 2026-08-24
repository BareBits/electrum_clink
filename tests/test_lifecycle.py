"""Unit tests for the plugin's wallet lifecycle (start/close/reopen).

Regression coverage for the field bug where closing *any* wallet — another
wallet's window, or a close-to-tray/reopen cycle — killed the CLINK listener
for the rest of the session while the UI stayed fully functional: offers could
still be created and probed, but no request was ever answered ("no response"
on every relay) until Electrum was fully restarted.

Uses real ``ClinkServer`` construction over fake wallets (so the whole
start/stop surface is exercised), with the asyncio scheduling hop
(``_run_async``) captured instead of run — no event loop or network needed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from clink.clink_plugin import ClinkPlugin, ClinkServer


class FakeConfig:
    """Just the config attributes ClinkServer touches at construction."""

    CLINK_RELAY = ""
    CLINK_INVOICE_EXPIRY = 120
    CLINK_DEVFEE_ENABLED = False
    CLINK_DEVFEE_RATE_PERCENT = 0.1
    CLINK_DEVFEE_DEST = ""

    def get_nostr_relays(self) -> List[str]:
        return ["wss://relay.example.com"]


def _wallet(path: str, *, lightning: bool = True, node_priv: bytes = b"\x01" * 32) -> Any:
    return SimpleNamespace(
        has_lightning=lambda: lightning,
        lnworker=SimpleNamespace(
            node_keypair=SimpleNamespace(privkey=node_priv), network=None),
        storage=SimpleNamespace(path=path),
        network=None,
        db=SimpleNamespace(get_plugin_storage=lambda: {}),
    )


def _plugin() -> ClinkPlugin:
    plugin = ClinkPlugin.__new__(ClinkPlugin)
    # BasePlugin.__init__ without the global hook registration (irrelevant here)
    from electrum.logging import Logger
    Logger.__init__(plugin)
    plugin.name = "clink"
    plugin.config = FakeConfig()
    plugin.server = None
    from electrum.util import OldTaskGroup
    plugin.taskgroup = OldTaskGroup()
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
    if p.server is not None:
        p.server.unregister_callbacks()


def test_start_creates_server_and_schedules_run(plugin: ClinkPlugin) -> None:
    wallet = _wallet("/w/a")
    plugin.start_plugin(wallet)
    assert isinstance(plugin.server, ClinkServer)
    assert plugin.server.wallet is wallet
    assert not plugin.server.do_stop
    assert len(plugin.scheduled) == 1


def test_start_without_lightning_is_refused(plugin: ClinkPlugin) -> None:
    plugin.start_plugin(_wallet("/w/a", lightning=False))
    assert plugin.server is None


def test_start_twice_same_wallet_is_idempotent(plugin: ClinkPlugin) -> None:
    wallet = _wallet("/w/a")
    plugin.start_plugin(wallet)
    first = plugin.server
    plugin.start_plugin(wallet)
    assert plugin.server is first
    assert len(plugin.scheduled) == 1


def test_second_wallet_file_is_ignored(plugin: ClinkPlugin) -> None:
    wallet_a = _wallet("/w/a")
    plugin.start_plugin(wallet_a)
    first = plugin.server
    plugin.start_plugin(_wallet("/w/b", node_priv=b"\x02" * 32))
    assert plugin.server is first  # single-wallet plugin: A keeps the slot
    assert plugin.server.wallet is wallet_a


def test_closing_another_wallet_leaves_server_running(plugin: ClinkPlugin) -> None:
    """THE field bug: closing any other wallet used to kill the listener."""
    wallet_a = _wallet("/w/a")
    plugin.start_plugin(wallet_a)
    server = plugin.server
    plugin.close_wallet(_wallet("/w/b", node_priv=b"\x02" * 32))
    assert plugin.server is server
    assert not server.do_stop


def test_closing_driven_wallet_stops_and_detaches_server(plugin: ClinkPlugin) -> None:
    wallet = _wallet("/w/a")
    plugin.start_plugin(wallet)
    server = plugin.server
    plugin.close_wallet(wallet)
    assert plugin.server is None
    assert server.do_stop
    # the async teardown (manager close + taskgroup cancel) was scheduled
    assert len(plugin.scheduled) == 2


def test_close_without_wallet_arg_stops_server(plugin: ClinkPlugin) -> None:
    # Defensive: a hook caller that passes no wallet still stops the server
    # (pre-fix behaviour, minus the stickiness).
    wallet = _wallet("/w/a")
    plugin.start_plugin(wallet)
    plugin.close_wallet()
    assert plugin.server is None


def test_close_then_reopen_restarts_the_server(plugin: ClinkPlugin) -> None:
    """After the driven wallet closes, a reload must bring CLINK back up —
    the old sticky ``initialized`` flag blocked this until a full restart."""
    wallet = _wallet("/w/a")
    plugin.start_plugin(wallet)
    old_taskgroup = plugin.taskgroup
    plugin.close_wallet(wallet)

    reopened = _wallet("/w/a")  # a fresh object, as Electrum builds on reload
    plugin.start_plugin(reopened)
    assert isinstance(plugin.server, ClinkServer)
    assert plugin.server.wallet is reopened
    assert not plugin.server.do_stop
    assert plugin.taskgroup is not old_taskgroup  # fresh group per generation


def test_daemon_reload_without_close_hook_swaps_the_server(plugin: ClinkPlugin) -> None:
    """Daemon ``close_wallet``/``load_wallet`` never fires the close hook; the
    reload is only visible as a new wallet object for the same file. The stale
    server (bound to the dead wallet) must be replaced, not kept."""
    wallet = _wallet("/w/a")
    plugin.start_plugin(wallet)
    stale = plugin.server

    reloaded = _wallet("/w/a")
    plugin.start_plugin(reloaded)
    assert plugin.server is not stale
    assert plugin.server.wallet is reloaded
    assert stale.do_stop  # the stale server was stopped, not leaked


def test_close_same_file_new_object_stops_stale_server(plugin: ClinkPlugin) -> None:
    """A daemon reload swaps the wallet object without firing Qt hooks; when
    the *reloaded* wallet later closes, the hook sees an object that is not
    ``server.wallet`` but IS the same wallet file. The stale server must be
    stopped — left running it is unreachable by any later hook and its run()
    task would hang the process-exit teardown forever."""
    plugin.start_plugin(_wallet("/w/a"))
    stale = plugin.server
    plugin.close_wallet(_wallet("/w/a"))  # same file, fresh object
    assert plugin.server is None
    assert stale.do_stop


def test_close_same_file_unknown_path_leaves_server(plugin: ClinkPlugin) -> None:
    """A wallet whose file path cannot be read is not proven to be ours —
    stay conservative and keep the listener running."""
    plugin.start_plugin(_wallet("/w/a"))
    server = plugin.server
    broken = _wallet("/w/a")
    broken.storage = None  # _wallet_file() -> None
    plugin.close_wallet(broken)
    assert plugin.server is server
    assert not server.do_stop


def test_on_close_stops_server(plugin: ClinkPlugin) -> None:
    """Disabling the plugin (BasePlugin.close -> on_close) unregisters every
    hook first, so this is the last chance to stop the listener; without it
    the server would keep answering offers and hang the eventual exit."""
    plugin.start_plugin(_wallet("/w/a"))
    server = plugin.server
    plugin.on_close()
    assert plugin.server is None
    assert server.do_stop


def test_stop_server_close_cancels_taskgroup_despite_manager_error(
        plugin: ClinkPlugin) -> None:
    """The scheduled close() must cancel the run() taskgroup even when the
    relay-manager teardown raises — otherwise run() outlives the stop."""
    import asyncio

    plugin.start_plugin(_wallet("/w/a"))
    server = plugin.server
    cancelled: List[bool] = []

    class ExplodingManager:
        async def close(self) -> None:
            raise RuntimeError("boom")

    class RecordingGroup:
        async def cancel_remaining(self) -> None:
            cancelled.append(True)

    server.manager = ExplodingManager()  # type: ignore[assignment]
    plugin.taskgroup = RecordingGroup()  # type: ignore[assignment]

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
    plugin.start_plugin(_wallet("/w/a"))
    pubkey_before = plugin.server.pubkey_hex
    plugin.close_wallet(plugin.server.wallet)
    plugin.start_plugin(_wallet("/w/a"))
    assert plugin.server.pubkey_hex == pubkey_before
