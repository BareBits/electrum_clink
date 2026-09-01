"""Unit tests for the heartbeat transport (silent listener death, part 1/3).

The field failure: after hours of uptime a NAT box or reverse proxy silently
culls the idle websocket to the relay. No FIN ever reaches us, so
``ws.receive_str()`` blocks forever, the listener goes deaf, and every noffer
stops being answered until the wallet restarts. The heartbeat turns that
half-open connection into a detected close: aiohttp pings the peer and tears
the connection down on a missed pong.

The centerpiece here is a real-socket test against a server that completes the
websocket handshake and then never sends another byte — in particular never a
pong — exactly like a frozen relay behind a live TCP path.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import socket
import threading
from typing import Any, Dict, Iterator, Tuple

import pytest
from aiohttp import ClientSession

from clink.nostr_transport import (
    DEFAULT_WS_HEARTBEAT_SEC,
    HeartbeatManager,
    HeartbeatRelay,
)

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --- a websocket server that goes silent after the handshake ------------------

def _accept_and_go_silent(conn: socket.socket) -> None:
    """Complete the websocket handshake, then read-and-discard forever.

    The TCP connection stays fully alive (the kernel keeps ACKing whatever the
    client sends, pings included) but no websocket frame — no pong, no close —
    is ever sent back: the application-layer half-open state."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return
        data += chunk
    key = ""
    for line in data.split(b"\r\n"):
        if line.lower().startswith(b"sec-websocket-key:"):
            key = line.split(b":", 1)[1].strip().decode()
    accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest())
    conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                 b"Upgrade: websocket\r\n"
                 b"Connection: Upgrade\r\n"
                 b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
    try:
        while conn.recv(4096):
            pass
    except OSError:
        pass


@pytest.fixture()
def silent_ws_url() -> Iterator[str]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = server.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        server.settimeout(0.2)
        conns = []
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=_accept_and_go_silent, args=(conn,), daemon=True)
            t.start()
            conns.append(conn)
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        stop.set()
        server.close()
        thread.join(timeout=5)


async def _wait_ws_closed(ws: Any, deadline_sec: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_sec
    while not ws.closed and loop.time() < deadline:
        await asyncio.sleep(0.05)
    return bool(ws.closed)


def test_heartbeat_tears_down_a_silent_connection(silent_ws_url: str) -> None:
    """A missed pong must close the websocket (making the death visible to the
    receive loop / watchdog) instead of blocking on it forever."""
    async def go() -> None:
        relay = HeartbeatRelay(silent_ws_url, heartbeat_sec=0.3, connect_timeout=5)
        assert await relay.connect(retries=1)
        ws = relay.ws
        assert await _wait_ws_closed(ws, deadline_sec=5), \
            "heartbeat did not detect the silent peer"
        await relay.close()
    asyncio.run(go())


def test_without_heartbeat_a_silent_connection_stays_open(silent_ws_url: str) -> None:
    """Control: documents the failure mode the heartbeat exists to close —
    with the heartbeat disabled the dead connection is never detected."""
    async def go() -> None:
        relay = HeartbeatRelay(silent_ws_url, heartbeat_sec=0, connect_timeout=5)
        assert await relay.connect(retries=1)
        await asyncio.sleep(1.0)
        assert not relay.ws.closed
        await relay.close()
    asyncio.run(go())


# --- the heartbeat reaches every ws_connect ----------------------------------

def test_ws_connect_carries_heartbeat_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    def fake_ws_connect(self: Any, *args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("handshake intercepted")

    monkeypatch.setattr(ClientSession, "ws_connect", fake_ws_connect)

    async def go() -> None:
        relay = HeartbeatRelay("ws://127.0.0.1:1", heartbeat_sec=9, connect_timeout=1)
        assert not await relay.connect(retries=1)
    asyncio.run(go())
    assert captured.get("heartbeat") == 9


def test_zero_heartbeat_disables_the_keepalive(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    def fake_ws_connect(self: Any, *args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("handshake intercepted")

    monkeypatch.setattr(ClientSession, "ws_connect", fake_ws_connect)

    async def go() -> None:
        relay = HeartbeatRelay("ws://127.0.0.1:1", heartbeat_sec=0, connect_timeout=1)
        assert not await relay.connect(retries=1)
    asyncio.run(go())
    assert "heartbeat" not in captured


# --- HeartbeatManager builds heartbeat relays everywhere ----------------------

def test_manager_constructs_heartbeat_relays() -> None:
    async def go() -> None:
        manager = HeartbeatManager(
            ["wss://a.example", "wss://b.example"], heartbeat_sec=7)
        assert len(manager.relays) == 2
        for relay in manager.relays:
            assert isinstance(relay, HeartbeatRelay)
            assert relay.heartbeat_sec == 7
    asyncio.run(go())


def test_manager_default_heartbeat() -> None:
    async def go() -> None:
        manager = HeartbeatManager(["wss://a.example"])
        assert manager.relays[0].heartbeat_sec == DEFAULT_WS_HEARTBEAT_SEC
    asyncio.run(go())


def test_manager_add_constructs_heartbeat_relay() -> None:
    async def go() -> None:
        manager = HeartbeatManager([], heartbeat_sec=7)
        manager.add("wss://c.example")
        assert isinstance(manager.relays[0], HeartbeatRelay)
        assert manager.relays[0].heartbeat_sec == 7
    asyncio.run(go())


def test_failed_update_relays_does_not_poison_live_subscription(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (found via the rig): base ``update_relays`` refreshes
    subscriptions even when zero relays are connected, and ``monitor_queues``
    over zero queues immediately emits the end-of-stream ``None`` sentinel —
    which a live (only_stored=False) ``get_events`` consumer reads as "done".
    In the field that silently killed the listener the moment a dead relay's
    replacement failed its first connect. A failed re-attach must leave the
    live subscription untouched."""
    from electrum_aionostr.relay import ManagerSubscription

    async def failing_connect(self: Any, taskgroup: Any = None,
                              retries: int = 2) -> bool:
        return False  # relay stays disconnected, like a still-dark relay

    monkeypatch.setattr(HeartbeatRelay, "connect", failing_connect)

    async def go() -> None:
        manager = HeartbeatManager([], heartbeat_sec=7)
        manager.connected = True
        output_queue: asyncio.Queue = asyncio.Queue()

        async def idle_monitor() -> None:
            await asyncio.Event().wait()

        manager.subscriptions["sub1"] = ManagerSubscription(
            output_queue=output_queue,
            filters=({"kinds": [21001]},),
            seen_events=set(),
            monitor=asyncio.create_task(idle_monitor()),
            only_stored=False,
        )
        await manager.update_relays(["wss://dark.example"])
        assert manager.relays == []
        # the poison would be an immediate None in the output queue
        assert output_queue.empty(), \
            "failed re-attach pushed the end-of-stream sentinel"
        manager.subscriptions["sub1"].monitor.cancel()
    asyncio.run(go())


def test_update_relays_constructs_heartbeat_relays(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The watchdog re-attaches lost relays via update_relays — the relays it
    builds must carry the heartbeat too, or a re-attached relay would silently
    lose the keepalive for the rest of the session."""
    async def fake_connect(self: Any, taskgroup: Any = None, retries: int = 2) -> bool:
        self.connected = True
        return True

    monkeypatch.setattr(HeartbeatRelay, "connect", fake_connect)

    async def go() -> None:
        manager = HeartbeatManager([], heartbeat_sec=7)
        manager.connected = True  # update_relays refuses on a virgin manager
        await manager.update_relays(["wss://d.example"])
        assert len(manager.relays) == 1
        relay = manager.relays[0]
        assert isinstance(relay, HeartbeatRelay)
        assert relay.heartbeat_sec == 7
    asyncio.run(go())
