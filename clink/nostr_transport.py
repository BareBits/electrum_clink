"""Heartbeat-enabled Nostr transport: keepalive websockets for the listener.

``electrum_aionostr``'s ``Relay`` opens its websocket without any keepalive and
only reconnects when the relay *cleanly* closes the connection. A NAT box,
reverse proxy, or relay host dying silently leaves a half-open TCP connection:
``ws.receive_str()`` then blocks forever, the listener goes deaf, and every
noffer stops being answered until the wallet restarts (found in the field
after long uptimes). The subclasses here inject aiohttp's built-in websocket
heartbeat — the client pings every ``heartbeat_sec`` seconds and aiohttp tears
the connection down on a missed pong — which turns silent death into an error
the library's reconnect path and the plugin's relay watchdog can see.

Kept as subclasses (not a patch to the installed ``electrum_aionostr``) so the
fix ships inside the plugin zip; the same change is worth upstreaming, since
Electrum's bundled NWC plugin shares this transport and its blind spot.
"""

from __future__ import annotations

import warnings
from typing import Any, Iterable, List, Optional, Set

from aiohttp import ClientSession

from electrum_aionostr.relay import Manager, NotInitialized, Relay
from electrum_aionostr.util import normalize_url

# Ping cadence for listener websockets. Detection latency for a dead peer is
# ~1.5x this (aiohttp waits half an interval for the pong), well under the
# watchdog's re-attach cadence. Public relays answer protocol-level pings in
# their websocket stack, so idle connections stay quiet apart from the pings.
DEFAULT_WS_HEARTBEAT_SEC = 30.0

with warnings.catch_warnings():
    # aiohttp discourages subclassing ClientSession with a DeprecationWarning;
    # this shim only injects a default ``ws_connect`` kwarg.
    warnings.simplefilter("ignore", DeprecationWarning)

    class _HeartbeatSession(ClientSession):
        """``ClientSession`` whose websockets default to a ping/pong heartbeat."""

        def __init__(self, *args: Any, ws_heartbeat: float, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._ws_heartbeat = ws_heartbeat

        def ws_connect(self, *args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("heartbeat", self._ws_heartbeat)
            return super().ws_connect(*args, **kwargs)


class HeartbeatRelay(Relay):
    """``Relay`` whose websocket carries a client-side heartbeat.

    ``heartbeat_sec <= 0`` disables the heartbeat (plain ``Relay`` behavior).
    """

    def __init__(self, url: str, *args: Any,
                 heartbeat_sec: float = DEFAULT_WS_HEARTBEAT_SEC,
                 **kwargs: Any) -> None:
        super().__init__(url, *args, **kwargs)
        self.heartbeat_sec = float(heartbeat_sec)

    async def connect(self, taskgroup: Any = None, retries: int = 2) -> bool:
        # Pre-create the session the base connect() would lazily create, so
        # its websockets get the heartbeat. The base failure/cancel paths null
        # ``self.client`` and come back through here, re-creating it.
        if self.client is None and self.heartbeat_sec > 0:
            self.client = _HeartbeatSession(
                ws_heartbeat=self.heartbeat_sec,
                connector=self.proxy,
                connector_owner=self.proxy is None)
        return await super().connect(taskgroup=taskgroup, retries=retries)


class HeartbeatManager(Manager):
    """``Manager`` that builds :class:`HeartbeatRelay` everywhere the base
    class would build a plain ``Relay`` (construction, ``add``, and
    ``update_relays``)."""

    def __init__(self, relays: Optional[Iterable[str]] = None, *args: Any,
                 heartbeat_sec: float = DEFAULT_WS_HEARTBEAT_SEC,
                 **kwargs: Any) -> None:
        self.heartbeat_sec = float(heartbeat_sec)
        super().__init__(relays, *args, **kwargs)
        self.relays = [self._new_relay(relay.url) for relay in self.relays]

    def _new_relay(self, url: str) -> HeartbeatRelay:
        return HeartbeatRelay(
            url,
            origin=self._origin,
            private_key=self._private_key,
            log=self.log,
            ssl_context=self._ssl_context,
            proxy=self._proxy,
            connect_timeout=self._connect_timeout,
            heartbeat_sec=self.heartbeat_sec)

    def add(self, url: str, **kwargs: Any) -> None:
        self.relays.append(self._new_relay(url))

    async def update_relays(self, updated_relay_list: Iterable[str]) -> None:
        # Copied from electrum_aionostr.relay.Manager.update_relays (0.1.0)
        # with the hardcoded ``Relay(...)`` swapped for ``self._new_relay()``;
        # the base method offers no relay-factory hook.
        if not self.connected:
            raise NotInitialized("Manager is not connected")

        changes: bool = False
        updated: Set[str] = set(normalize_url(url) for url in updated_relay_list)
        self.log.debug(f"Updating relays, new list: {updated}")
        new_relays: List[HeartbeatRelay] = []
        for relay_url in updated:
            if relay_url in [relay.url for relay in self.relays]:
                continue
            new_relays.append(self._new_relay(relay_url))
        if new_relays:
            changes = True
            async with self._connectlock:
                await self.broadcast(new_relays, 'connect', self.taskgroup)
                connected_relays = [relay for relay in new_relays if relay.connected]
                self.relays.extend(connected_relays)
                self.log.info("Connected to %d out of %d new relays",
                              len(connected_relays), len(new_relays))

        remove_relays: List[Relay] = [
            relay for relay in self.relays if relay.url not in updated]
        if remove_relays:
            changes = True
            async with self._connectlock:
                await self.broadcast(remove_relays, 'close', self.taskgroup)
                self.relays = [relay for relay in self.relays
                               if relay not in remove_relays]
                self.log.info("Removed %d relays", len(remove_relays))

        # Deviation from the base method: never refresh subscriptions while NO
        # relay is connected (e.g. the one relay died and its replacement
        # failed to connect). ``monitor_queues`` over zero queues immediately
        # emits the end-of-stream ``None`` sentinel, which a live
        # (only_stored=False) ``get_events`` consumer reads as "done" — it
        # unsubscribes and exits, silently killing the listener for good. The
        # attempt that finally connects a relay runs the refresh instead.
        if changes and self.relays:
            for sub_id, subscription in self.subscriptions.items():
                await self.subscribe(sub_id, subscription.only_stored,
                                     *subscription.filters)
