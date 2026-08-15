"""Payability probe for the single Nostr relay a noffer advertises.

A ``noffer`` embeds exactly one relay (see :mod:`clink.noffer`), and the
reference ``@shocknet/clink-sdk`` payer connects to *only* that relay
(``SendNofferRequest(pool, sk, [data.relay], ...)``). So a noffer is payable
only if its single relay round-trips a live kind-21001 request between an
arbitrary payer and the receiver. Public relays commonly refuse ephemeral /
non-standard event kinds, require NIP-42 auth, or are paid — and the noffer
format has no room for a fallback. Picking a dead relay yields a noffer that
looks fine but is silently unpayable (the failure this module exists to catch).

``probe_relay_payable`` verifies that predicate with a minimal, self-contained
write/read-back round-trip: an *independent* subscriber must receive a freshly
published kind-21001 event. CLINK events live in the NIP-16 ephemeral range
(20000-29999), so relays never store them — the subscriber must already be live
*before* the event is published, exactly mirroring the real payer/receiver
handshake (and exactly why a plain "does the socket open" check is not enough).

``select_payable_relay`` orchestrates the probe over a candidate list and is a
pure function of an injected ``probe`` coroutine, so it is unit-testable with no
network. Both the runtime auto-pick and the test suite import these, so
"payable" means one single thing everywhere.
"""

from __future__ import annotations

import asyncio
import enum
import secrets
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional, Sequence
from urllib.parse import urlsplit

import electrum_aionostr as aionostr
from electrum_aionostr.key import PrivateKey

# CLINK request/response event kind. Duplicated from clink_plugin (rather than
# imported) so this module carries no dependency on the plugin runtime.
CLINK_EVENT_KIND = 21001

# Default per-relay budget for the whole connect->publish->read-back round-trip.
DEFAULT_PROBE_TIMEOUT = 8.0
# Grace period so the relay registers our subscription before we publish the
# ephemeral event (which it would otherwise drop, having no live subscriber).
SUBSCRIBE_SETTLE_SEC = 0.3
# Marker embedded in the probe event's content so a shared relay can't hand us
# an unrelated event and pass the probe by accident.
PROBE_CONTENT_PREFIX = "clink-relay-probe:"


def normalize_relay_url(url: str) -> str:
    """Validate a user-supplied relay URL (``wss://myrelay.com:port``).

    Returns the stripped URL, or raises ``ValueError`` naming the problem.
    Shared by the Qt "New offer" dialog and the CLI so a typo'd relay is
    refused before an unpayable noffer is built. ``ws://`` is accepted too
    (local/regtest relays); anything else — wrong scheme, no host, bad port,
    embedded whitespace — is rejected.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise ValueError("relay URL is empty")
    if any(c.isspace() for c in candidate):
        raise ValueError(f"relay URL contains whitespace: {candidate!r}")
    parts = urlsplit(candidate)
    if parts.scheme not in ("ws", "wss"):
        raise ValueError(
            f"relay URL must start with wss:// or ws:// (got {candidate!r})")
    if not parts.hostname:
        raise ValueError(f"relay URL has no host: {candidate!r}")
    try:
        _ = parts.port  # the property raises ValueError on a malformed port
    except ValueError:
        raise ValueError(f"relay URL has an invalid port: {candidate!r}") from None
    return candidate


class ProbeStatus(enum.Enum):
    """Outcome of a single relay payability probe."""

    OK = "ok"                     # published and read back -> payable
    UNREACHABLE = "unreachable"   # could not open a websocket to the relay
    NO_READBACK = "no_readback"   # connected + published but never received it
    ERROR = "error"               # unexpected failure while probing


@dataclass
class ProbeResult:
    """Result of probing one relay."""

    relay: str
    status: ProbeStatus
    rtt_ms: Optional[int] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is ProbeStatus.OK


@dataclass
class RelaySelection:
    """The relay chosen for a noffer, plus every candidate's probe result."""

    relay: str
    ok: bool
    results: List[ProbeResult] = field(default_factory=list)

    def failures(self) -> List[ProbeResult]:
        return [r for r in self.results if not r.ok]


async def _await_readback(reader: Any, reader_pub: str, marker: str) -> bool:
    """Yield ``True`` once the reader's live subscription sees our probe event."""
    query = {
        "kinds": [CLINK_EVENT_KIND],
        "#p": [reader_pub],
        "since": int(time.time()) - 5,
        "limit": 0,
    }
    async for event in reader.get_events(query, single_event=True, only_stored=False):
        if marker in (getattr(event, "content", "") or ""):
            return True
    return False


async def _safe_close(manager: Any) -> None:
    try:
        await manager.close()
    except Exception:
        pass


async def _run_probe(
    relay: str,
    reader: Any,
    writer: Any,
    *,
    reader_pub: str,
    marker: str,
    publish: Callable[[], Awaitable[Any]],
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    settle: float = SUBSCRIBE_SETTLE_SEC,
    clock: Callable[[], float] = time.monotonic,
) -> ProbeResult:
    """Drive the connect -> subscribe -> publish -> read-back round-trip.

    Split from :func:`probe_relay_payable` so the ordering logic can be
    unit-tested against fake managers without any network.
    """
    start = clock()
    read_task: Optional["asyncio.Task[bool]"] = None
    try:
        try:
            await asyncio.wait_for(reader.connect(), timeout)
            await asyncio.wait_for(writer.connect(), timeout)
        except asyncio.TimeoutError:
            return ProbeResult(relay, ProbeStatus.UNREACHABLE, detail="connect timed out")
        # Manager.connect() drops relays it could not reach, so an empty relay
        # set (or a still-disconnected manager) means "unreachable".
        for who, mgr in (("reader", reader), ("writer", writer)):
            if not getattr(mgr, "relays", None) or not getattr(mgr, "connected", False):
                return ProbeResult(relay, ProbeStatus.UNREACHABLE,
                                   detail=f"{who} could not connect")

        read_task = asyncio.ensure_future(_await_readback(reader, reader_pub, marker))
        # Let the subscription settle before publishing the ephemeral event.
        await asyncio.sleep(settle)
        await publish()

        try:
            got = await asyncio.wait_for(read_task, timeout)
        except asyncio.TimeoutError:
            return ProbeResult(relay, ProbeStatus.NO_READBACK,
                               detail="timed out waiting for read-back")
        rtt_ms = int((clock() - start) * 1000)
        if got:
            return ProbeResult(relay, ProbeStatus.OK, rtt_ms=rtt_ms)
        return ProbeResult(relay, ProbeStatus.NO_READBACK, rtt_ms=rtt_ms,
                           detail="relay did not deliver the probe event")
    except Exception as e:  # noqa: BLE001 - any probe failure is just "not ok"
        return ProbeResult(relay, ProbeStatus.ERROR, detail=repr(e))
    finally:
        if read_task is not None and not read_task.done():
            read_task.cancel()
            try:
                await read_task
            except BaseException:
                pass
        await _safe_close(reader)
        await _safe_close(writer)


async def probe_relay_payable(
    relay: str,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    ssl_context: Optional[ssl.SSLContext] = None,
    proxy_factory: Optional[Callable[[], Any]] = None,
) -> ProbeResult:
    """Return whether ``relay`` can carry a CLINK payment round-trip.

    Uses two throwaway identities on independent connections: a reader that
    subscribes and a writer that publishes a kind-21001 event to it. A fresh
    proxy connector is built per manager (via ``proxy_factory``) because an
    aiohttp connector cannot be shared across sessions.
    """
    reader_sk = PrivateKey()
    writer_sk = PrivateKey()
    reader_pub = reader_sk.public_key.hex()
    marker = secrets.token_hex(8)

    def make_manager(private_key: str) -> aionostr.Manager:
        return aionostr.Manager(
            relays=[relay],
            private_key=private_key,
            ssl_context=ssl_context,
            proxy=proxy_factory() if proxy_factory else None,
        )

    reader = make_manager(reader_sk.hex())
    writer = make_manager(writer_sk.hex())

    async def publish() -> Any:
        return await aionostr._add_event(
            writer,
            kind=CLINK_EVENT_KIND,
            tags=[["p", reader_pub]],
            content=f"{PROBE_CONTENT_PREFIX}{marker}",
            private_key=writer_sk.hex(),
        )

    return await _run_probe(
        relay, reader, writer,
        reader_pub=reader_pub, marker=marker, publish=publish, timeout=timeout,
    )


async def select_payable_relay(
    candidates: Sequence[str],
    *,
    probe: Callable[[str], Awaitable[ProbeResult]],
) -> RelaySelection:
    """Probe every candidate and pick the first (in order) that is payable.

    Pure orchestration over an injected ``probe`` coroutine — no network here, so
    it is unit-testable. Candidates are probed concurrently for speed, but the
    winner is chosen by the input order (a stable, config-driven preference). If
    none pass, the first candidate is returned with ``ok=False`` so the caller
    can still build a noffer while surfacing that it may be unpayable.
    """
    cands = [c.strip() for c in candidates if c and c.strip()]
    if not cands:
        return RelaySelection(relay="", ok=False, results=[])
    results = list(await asyncio.gather(*(probe(c) for c in cands)))
    for res in results:  # asyncio.gather preserves input order
        if res.ok:
            return RelaySelection(relay=res.relay, ok=True, results=results)
    return RelaySelection(relay=cands[0], ok=False, results=results)
