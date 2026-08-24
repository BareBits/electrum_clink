"""Shutdown/cancellation semantics of ``ClinkServer.run()``.

Regression coverage for the process-exit hang: Electrum's event-loop teardown
(``electrum.util.run_event_loop``) cancels every remaining task exactly once
and then waits for all of them on the non-daemon EventLoop thread. The old
restart-on-cancel loop in ``run()`` swallowed that one cancel whenever
``do_stop`` was False and respawned fresh listener tasks — leaving an
unkillable task, a never-ending EventLoop thread, and a process that only
``pkill`` could stop.

Covers, over a fake relay manager (no network):

  * a foreign ``.cancel()`` of the run() task ends it — the hang regression
  * ``restart_event_handler`` still rebuilds the listener (restarts ride a
    normal taskgroup exit, not the CancelledError path)
  * the plugin-stop path (``do_stop`` + cancel) ends the task

plus a subprocess end-to-end test that boots Electrum's real event-loop
machinery and performs the exact ``run_electrum`` shutdown sequence, with and
without the ``close_wallet`` hook having fired.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any, Tuple

import pytest

from clink.clink_plugin import ClinkServer

from tests.test_lifecycle import _plugin, _wallet


class FakeManager:
    """Relay manager whose event stream never yields (idle live listener)."""

    def __init__(self) -> None:
        self.connected = True
        self.relays = ["wss://relay.example.com"]
        self.listen_entered = asyncio.Event()
        self.enter_count = 0

    async def get_events(self, query: Any, single_event: bool = False,
                         only_stored: bool = False):
        self.enter_count += 1
        self.listen_entered.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover  (makes this an async generator)

    async def close(self) -> None:
        pass


def _listening_server() -> Tuple[ClinkServer, FakeManager]:
    """A real ClinkServer steered into the listening state, network-free."""
    plugin = _plugin()
    plugin.start_plugin(_wallet("/w/a"))
    server = plugin.server
    assert server is not None
    server.wallet.network = type(  # listener_prerequisites_met needs it
        "N", (), {"is_connected": staticmethod(lambda: True)})()
    manager = FakeManager()

    async def _noop() -> None:
        return None

    server.ensure_offer_relays_pinned = _noop  # type: ignore[method-assign]
    server.listener_prerequisites_met = lambda: True  # type: ignore[method-assign]

    async def _refresh() -> bool:
        if server.manager is None:
            server.manager = manager  # type: ignore[assignment]
        return True

    server.refresh_manager = _refresh  # type: ignore[method-assign]
    return server, manager


async def _reap(server: ClinkServer, task: "asyncio.Task[None]") -> None:
    """Make ``task`` stoppable and wait it out (cleanup for failure paths)."""
    server.do_stop = True
    task.cancel()
    await asyncio.wait({task}, timeout=5)
    server.unregister_callbacks()


def test_foreign_cancel_ends_run_task() -> None:
    """THE exit-hang regression: one .cancel() — as delivered by Electrum's
    loop teardown, with do_stop still False — must end the run() task."""
    async def main() -> None:
        server, manager = _listening_server()
        task = asyncio.create_task(server.run())
        await asyncio.wait_for(manager.listen_entered.wait(), timeout=5)

        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=5)
        survived = task not in done
        if survived:
            await _reap(server, task)  # keep the loop drainable before failing
        else:
            server.unregister_callbacks()
        assert not survived, (
            "run() swallowed a foreign cancel and restarted itself — this "
            "hangs Electrum's process exit")
        assert task.cancelled()

    asyncio.run(main())


def test_restart_event_handler_rebuilds_listener() -> None:
    """The designed restart path must survive the fix: cancelling the inner
    taskgroup's children rebuilds the listener without killing run()."""
    async def main() -> None:
        import electrum.util as electrum_util

        server, manager = _listening_server()
        saved_loop = electrum_util._asyncio_event_loop
        electrum_util._asyncio_event_loop = asyncio.get_running_loop()
        task = asyncio.create_task(server.run())
        try:
            await asyncio.wait_for(manager.listen_entered.wait(), timeout=5)
            manager.listen_entered.clear()

            server.restart_event_handler()
            await asyncio.wait_for(manager.listen_entered.wait(), timeout=5)
            assert manager.enter_count == 2
            assert not task.done()
        finally:
            await _reap(server, task)
            electrum_util._asyncio_event_loop = saved_loop
        assert task.done()

    asyncio.run(main())


def test_plugin_stop_path_ends_run_task() -> None:
    """What _stop_server's close() does: do_stop=True, then cancel."""
    async def main() -> None:
        server, manager = _listening_server()
        task = asyncio.create_task(server.run())
        await asyncio.wait_for(manager.listen_entered.wait(), timeout=5)

        server.do_stop = True
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=5)
        server.unregister_callbacks()
        assert task in done

    asyncio.run(main())


# --- end-to-end: Electrum's real loop machinery and shutdown sequence --------

_HARNESS = Path(__file__).resolve().parent / "exit_hang_harness.py"
_ELECTRUM_DIR = Path(__file__).resolve().parents[2] / "electrum"


@pytest.mark.parametrize("mode", ["hook", "nohook"])
def test_process_exit_teardown(mode: str) -> None:
    """Boot the real non-daemon EventLoop thread, run the real plugin over a
    fake manager, then perform run_electrum's exact shutdown sequence. The
    thread must finish whether or not the close_wallet hook fired; 'nohook'
    used to hang forever (only pkill helped)."""
    if not (_ELECTRUM_DIR / "electrum" / "util.py").exists():
        pytest.skip("sibling electrum checkout not found")
    result = subprocess.run(
        [sys.executable, str(_HARNESS), mode],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"exit teardown ({mode}) failed:\n{result.stdout}\n{result.stderr}")
