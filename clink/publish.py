"""Relay publishes that surface NIP-20 ``OK`` verdicts.

``electrum_aionostr``'s ``Relay.add_event(check_response=True)`` waits for the
relay's ``["OK", <event id>, <accepted>, <reason>]`` acknowledgment but ignores
the ``accepted`` flag: a relay that *rejects* an event (kind not allowed, rate
limited, auth required, paid relay) is indistinguishable from one that accepted
it. For CLINK that turns "the relay refused the request" into a silent
"no response" — the noffer self-test misclassifies the failure and the server
log never shows why a response was dropped.

:func:`add_event_checked` publishes through the same ``Relay`` objects but
reads the whole ``OK`` message, so a rejection becomes an explicit
:class:`PublishRejected` carrying the relay's reason. Semantics per relay:

  * ``OK … true``  from any relay  -> success (the event id is returned)
  * ``OK … false`` from every responding relay -> :class:`PublishRejected`
  * no ``OK`` at all before the timeout -> ``asyncio.TimeoutError`` (the same
    outcome ``Manager.add_event`` produces today, so OK-less relays keep their
    existing behaviour)

The helper leans on two small, stable internals of ``electrum_aionostr.Relay``
(``event_adds`` and ``send``); if either is missing (a future refactor) it
falls back to the stock ``aionostr._add_event`` rather than breaking publishes.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, List, Optional, Tuple

import electrum_aionostr as aionostr
from electrum_aionostr.event import Event
from electrum_aionostr.key import PrivateKey

# How long to wait for a relay's OK acknowledgment. Mirrors the default
# connect timeout Manager.add_event applies to the same wait.
DEFAULT_OK_TIMEOUT = 5.0


class PublishRejected(Exception):
    """Every relay that acknowledged the event refused to accept it."""


def _sign_event(*, kind: int, tags: List[List[str]], content: str,
                private_key: str) -> Event:
    """Build and sign the event exactly like ``aionostr._add_event`` does."""
    prikey = PrivateKey(bytes.fromhex(private_key))
    event = Event(
        pubkey=prikey.public_key.hex(),
        content=content,
        created_at=int(time.time()),
        tags=tags or [],
        kind=kind,
    )
    return event.sign(prikey.hex())


def _supports_checked_publish(relay: Any) -> bool:
    return isinstance(getattr(relay, "event_adds", None), dict) \
        and callable(getattr(relay, "send", None))


async def _publish_to_relay(relay: Any, event: Event,
                            timeout: float) -> Tuple[Optional[bool], str]:
    """Publish ``event`` to one relay and read its OK verdict.

    Returns ``(accepted, reason)`` where ``accepted`` is ``None`` when the
    relay never acknowledged (timeout / connection trouble).
    """
    fut: "asyncio.Future[list]" = asyncio.get_running_loop().create_future()
    relay.event_adds[event.id] = fut
    try:
        await relay.send(["EVENT", event.to_json_object()])
        message = await asyncio.wait_for(fut, timeout)
    except Exception as e:  # timeout or connection trouble; never an OK verdict
        return None, repr(e)
    finally:
        if relay.event_adds.get(event.id) is fut:
            del relay.event_adds[event.id]
    # NIP-20: ["OK", <event id>, <true|false>, <reason>]
    accepted = bool(message[2]) if len(message) > 2 else True
    reason = str(message[3]) if len(message) > 3 else ""
    return accepted, reason


async def add_event_checked(manager: Any, *, kind: int, tags: List[List[str]],
                            content: str, private_key: str,
                            timeout: float = DEFAULT_OK_TIMEOUT) -> str:
    """Publish an event via ``manager`` and honor the relays' OK verdicts.

    Returns the event id once any relay accepts. Raises
    :class:`PublishRejected` when every acknowledging relay refused, and
    ``asyncio.TimeoutError`` when no relay acknowledged at all (matching the
    stock ``Manager.add_event`` behaviour for OK-less relays).
    """
    relays = list(getattr(manager, "relays", None) or [])
    if not relays:
        raise asyncio.TimeoutError("no connected relays to publish to")
    if not all(_supports_checked_publish(r) for r in relays):
        # Unknown Relay internals (newer electrum_aionostr?): keep publishing
        # rather than failing, at the cost of the OK verdict.
        return await aionostr._add_event(
            manager, kind=kind, tags=tags, content=content,
            private_key=private_key)

    event = _sign_event(kind=kind, tags=tags, content=content,
                        private_key=private_key)
    results = await asyncio.gather(
        *(_publish_to_relay(relay, event, timeout) for relay in relays))

    if any(accepted is True for accepted, _ in results):
        return event.id
    rejections = [reason for accepted, reason in results if accepted is False]
    if rejections:
        detail = next((r for r in rejections if r), "no reason given")
        raise PublishRejected(f"relay rejected the event: {detail}")
    raise asyncio.TimeoutError("no relay acknowledged the event")
