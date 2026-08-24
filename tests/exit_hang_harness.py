"""Subprocess harness for the process-exit regression test.

Boots Electrum's real asyncio event-loop machinery (the non-daemon EventLoop
thread from ``electrum.util.create_and_start_event_loop``), runs the real
``ClinkServer.run()`` over a fake relay manager, then performs the exact
shutdown sequence ``run_electrum`` performs: set the stopping future, after
which the EventLoop thread cancels every task once and waits for all of them.

Usage: ``python exit_hang_harness.py {hook|nohook}``

  hook    the ``close_wallet`` hook fired before quitting (the clean path)
  nohook  no hook fired (stale server / disabled plugin / hook lost) — the
          path that used to hang the process forever

Either way the EventLoop thread must finish; exit code 0 iff it did.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_ELECTRUM_DIR = _PLUGIN_ROOT.parent / "electrum"
sys.path.insert(0, str(_ELECTRUM_DIR))
sys.path.insert(0, str(_PLUGIN_ROOT))

from electrum.logging import Logger
from electrum.util import create_and_start_event_loop

from clink.clink_plugin import ClinkPlugin

JOIN_TIMEOUT_SEC = 15.0


class FakeConfig:
    CLINK_RELAY = ""
    CLINK_INVOICE_EXPIRY = 120
    CLINK_DEVFEE_ENABLED = False
    CLINK_DEVFEE_RATE_PERCENT = 0.1
    CLINK_DEVFEE_DEST = ""

    def get_nostr_relays(self) -> List[str]:
        return ["wss://relay.example.com"]


def _wallet() -> Any:
    return SimpleNamespace(
        has_lightning=lambda: True,
        lnworker=SimpleNamespace(
            node_keypair=SimpleNamespace(privkey=b"\x01" * 32), network=None),
        storage=SimpleNamespace(path="/w/a"),
        network=SimpleNamespace(is_connected=lambda: True),
        db=SimpleNamespace(get_plugin_storage=lambda: {}),
    )


class FakeManager:
    """Relay manager whose event stream never yields (idle live listener)."""

    def __init__(self) -> None:
        self.connected = True
        self.relays = ["wss://relay.example.com"]

    async def get_events(self, query: Any, single_event: bool = False,
                         only_stored: bool = False):
        await asyncio.Event().wait()
        yield  # pragma: no cover  (makes this an async generator)

    async def close(self) -> None:
        pass


def main(fire_hook: bool) -> int:
    loop, stopping_fut, loop_thread = create_and_start_event_loop()

    plugin = ClinkPlugin.__new__(ClinkPlugin)
    Logger.__init__(plugin)
    plugin.name = "clink"
    plugin.config = FakeConfig()  # type: ignore[assignment]
    plugin.server = None
    from electrum.util import OldTaskGroup
    plugin.taskgroup = OldTaskGroup()

    wallet = _wallet()
    plugin.start_plugin(wallet)
    server = plugin.server
    assert server is not None

    # Steer run() into the listening state without touching the network.
    async def _noop() -> None:
        return None

    server.ensure_offer_relays_pinned = _noop  # type: ignore[method-assign]
    server.listener_prerequisites_met = lambda: True  # type: ignore[method-assign]

    async def _refresh() -> bool:
        if server.manager is None:
            server.manager = FakeManager()  # type: ignore[assignment]
        return True

    server.refresh_manager = _refresh  # type: ignore[method-assign]

    time.sleep(1.0)  # let run() enter its taskgroup and block in get_events

    if fire_hook:
        plugin.close_wallet(wallet)  # what the GUI quit path does
        time.sleep(1.0)             # let the scheduled close() run

    # run_electrum's sys_exit(): unblock the loop, which then cancels every
    # remaining task ONCE and waits for all of them before the thread ends.
    loop.call_soon_threadsafe(stopping_fut.set_result, 1)
    loop_thread.join(timeout=JOIN_TIMEOUT_SEC)

    if loop_thread.is_alive():
        print(f"FAIL: EventLoop thread still alive {JOIN_TIMEOUT_SEC}s after "
              f"stop (do_stop={server.do_stop}); process would hang", flush=True)
        return 1
    print("PASS: EventLoop thread finished; process exits cleanly", flush=True)
    return 0


if __name__ == "__main__":
    import os

    mode = sys.argv[1]
    assert mode in ("hook", "nohook"), mode
    # os._exit: the verdict is already printed, and on the FAIL path the
    # still-alive non-daemon EventLoop thread would block a normal exit —
    # the very bug under test — turning a clean FAIL into a subprocess hang.
    os._exit(main(fire_hook=(mode == "hook")))
