"""Round-trip self-test for a noffer — the engine behind "Check noffers".

A noffer looks fine long after it has silently stopped working: the relay it
embeds can die, start rejecting kind-21001 events, or the plugin's own listener
can be down — and the merchant only finds out when payers give up. This module
verifies a noffer end to end by *being* a payer: decode the noffer, connect to
its embedded relay with a throwaway identity, publish a real NIP-44-encrypted
offer request, and wait for the service's encrypted reply.

Unlike :mod:`clink.relay_probe` (which checks that a relay can carry kind-21001
at all, using synthetic events), this exercises the actual production path:
the plugin's live subscription, request decryption, offer lookup, invoice
issuance and response encryption. The caller registers the throwaway payer
pubkey with the running :class:`~clink.clink_plugin.ClinkServer` first, so the
service recognises the request as a self-test and unwinds its side effects
(no lingering liquidity lock, wallet request, dev-fee or receipt entries).

The payer subscribes *before* publishing: CLINK events are NIP-16 ephemeral,
so a relay only forwards them to already-live subscriptions — subscribing
after publishing (as a payer that trusts the response to be slow can) would
race a fast responder. Mirrors the ordering rationale in ``relay_probe``.

Like ``relay_probe``, the core (:func:`_run_check`) is a pure function of
injected managers/callables so it is unit-testable with no network, and the
module stays free of Electrum imports (the bolt11 validator is injected).
"""

from __future__ import annotations

import asyncio
import enum
import json
import ssl
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

import electrum_aionostr as aionostr
from electrum_aionostr.key import PrivateKey

from . import nip44
from .noffer import Noffer, noffer_decode

# CLINK request/response event kind (same value as clink_plugin; duplicated so
# this module carries no dependency on the plugin runtime).
CLINK_EVENT_KIND = 21001
CLINK_VERSION = "1"

# Budget for the whole connect -> subscribe -> publish -> response round-trip.
# A shade above the probe's 8s: this path additionally waits on the service to
# mint an invoice.
DEFAULT_CHECK_TIMEOUT = 12.0
# Grace period so the relay registers our subscription before we publish the
# ephemeral request (see module docstring).
SUBSCRIBE_SETTLE_SEC = 0.3
# Amount requested in the self-test. The smallest the protocol allows, so the
# momentary liquidity reservation on our own wallet is negligible.
SELFTEST_AMOUNT_SAT = 1

# A validator receives the bolt11 string and the amount we asked for; it
# returns None when the invoice is acceptable, or a short human-readable
# problem description. Injected so this module needs no Electrum imports.
Bolt11Validator = Callable[[str, int], Optional[str]]


class CheckStatus(enum.Enum):
    """Outcome of one noffer round-trip self-test."""

    OK = "ok"                          # invoice received and validated
    LISTENER_ERROR = "listener_error"  # round-trip worked; service sent a CLINK error
    UNREACHABLE = "unreachable"        # could not open a websocket to the relay
    PUBLISH_FAILED = "publish_failed"  # connected, but the relay refused the request
    NO_RESPONSE = "no_response"        # published, but no reply before the timeout
    BAD_RESPONSE = "bad_response"      # a reply arrived but was invalid
    INVALID_NOFFER = "invalid_noffer"  # the noffer string itself does not decode
    ERROR = "error"                    # unexpected failure while checking


@dataclass
class CheckResult:
    """Result of self-testing one noffer."""

    noffer: str
    status: CheckStatus
    detail: str = ""
    rtt_ms: Optional[int] = None
    error_code: Optional[int] = None   # CLINK error code for LISTENER_ERROR
    checked_at: float = 0.0            # epoch seconds; stamped by the caller

    @property
    def ok(self) -> bool:
        return self.status is CheckStatus.OK

    def to_dict(self) -> Dict[str, Any]:
        return {
            "noffer": self.noffer,
            "status": self.status.value,
            "ok": self.ok,
            "detail": self.detail,
            "rtt_ms": self.rtt_ms,
            "error_code": self.error_code,
            "checked_at": self.checked_at,
        }


async def _await_response(
    manager: Any,
    *,
    payer_sk: PrivateKey,
    receiver_pub: str,
    payer_pub: str,
    request_id_fut: "asyncio.Future[str]",
) -> Dict[str, Any]:
    """Yield the first decryptable response payload addressed to the payer.

    The subscription is opened before the request is published, so it cannot
    filter on the (not yet known) request event id; instead it awaits the id
    via ``request_id_fut`` and matches on the ``e`` tag once available, skipping
    unrelated or undecryptable events rather than failing on them.
    """
    query = {
        "kinds": [CLINK_EVENT_KIND],
        "#p": [payer_pub],
        "since": int(time.time()) - 5,
        "limit": 0,
    }
    async for event in manager.get_events(query, single_event=False, only_stored=False):
        if getattr(event, "pubkey", None) != receiver_pub:
            continue
        request_id = await request_id_fut
        etags = [t[1] for t in (getattr(event, "tags", None) or [])
                 if len(t) >= 2 and t[0] == "e"]
        if request_id not in etags:
            continue
        try:
            plaintext = nip44.decrypt_from(
                payer_sk.raw_secret, receiver_pub, event.content)
            payload = json.loads(plaintext)
            if not isinstance(payload, dict):
                raise ValueError("response is not a JSON object")
        except Exception:
            continue  # not for us / garbled; keep listening until the timeout
        return payload
    raise RuntimeError("subscription closed before a response arrived")


async def _safe_close(manager: Any) -> None:
    try:
        await manager.close()
    except Exception:
        pass


async def _run_check(
    noffer: Noffer,
    noffer_str: str,
    manager: Any,
    *,
    payer_sk: PrivateKey,
    publish: Callable[[], Awaitable[str]],
    amount_sat: int = SELFTEST_AMOUNT_SAT,
    validate_bolt11: Optional[Bolt11Validator] = None,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
    settle: float = SUBSCRIBE_SETTLE_SEC,
    clock: Callable[[], float] = time.monotonic,
) -> CheckResult:
    """Drive the connect -> subscribe -> publish -> response round-trip.

    Split from :func:`check_noffer` so the ordering and classification logic can
    be unit-tested against fake managers with no network. ``publish`` sends the
    encrypted request and returns its event id.
    """
    payer_pub = payer_sk.public_key.hex()
    start = clock()
    response_task: Optional["asyncio.Task[Dict[str, Any]]"] = None
    loop = asyncio.get_running_loop()
    request_id_fut: "asyncio.Future[str]" = loop.create_future()
    try:
        try:
            await asyncio.wait_for(manager.connect(), timeout)
        except asyncio.TimeoutError:
            return CheckResult(noffer_str, CheckStatus.UNREACHABLE,
                               detail="connect timed out")
        # Manager.connect() drops relays it could not reach, so an empty relay
        # set (or a still-disconnected manager) means "unreachable".
        if not getattr(manager, "relays", None) or not getattr(manager, "connected", False):
            return CheckResult(noffer_str, CheckStatus.UNREACHABLE,
                               detail="could not connect to relay")

        response_task = asyncio.ensure_future(_await_response(
            manager,
            payer_sk=payer_sk,
            receiver_pub=noffer.pubkey,
            payer_pub=payer_pub,
            request_id_fut=request_id_fut,
        ))
        # Let the subscription settle before publishing the ephemeral request.
        await asyncio.sleep(settle)
        try:
            request_id = await asyncio.wait_for(publish(), timeout)
            request_id_fut.set_result(request_id)
        except Exception as e:
            return CheckResult(noffer_str, CheckStatus.PUBLISH_FAILED,
                               detail=f"relay did not accept the request: {e!r:.120}")

        try:
            payload = await asyncio.wait_for(response_task, timeout)
        except asyncio.TimeoutError:
            return CheckResult(noffer_str, CheckStatus.NO_RESPONSE,
                               detail="no reply before the timeout")
        rtt_ms = int((clock() - start) * 1000)

        bolt11 = payload.get("bolt11")
        if isinstance(bolt11, str) and bolt11:
            problem = validate_bolt11(bolt11, amount_sat) if validate_bolt11 else None
            if problem:
                return CheckResult(noffer_str, CheckStatus.BAD_RESPONSE,
                                   rtt_ms=rtt_ms, detail=problem)
            return CheckResult(noffer_str, CheckStatus.OK, rtt_ms=rtt_ms)
        if "code" in payload or "error" in payload:
            code = payload.get("code")
            return CheckResult(
                noffer_str, CheckStatus.LISTENER_ERROR, rtt_ms=rtt_ms,
                error_code=code if isinstance(code, int) else None,
                detail=str(payload.get("error") or "")[:120],
            )
        return CheckResult(noffer_str, CheckStatus.BAD_RESPONSE, rtt_ms=rtt_ms,
                           detail="response carried neither an invoice nor an error")
    except Exception as e:  # noqa: BLE001 - any check failure is just "not ok"
        return CheckResult(noffer_str, CheckStatus.ERROR, detail=repr(e))
    finally:
        if not request_id_fut.done():
            request_id_fut.cancel()
        if response_task is not None and not response_task.done():
            response_task.cancel()
            try:
                await response_task
            except BaseException:
                pass
        await _safe_close(manager)


async def check_noffer(
    noffer_str: str,
    *,
    amount_sat: int = SELFTEST_AMOUNT_SAT,
    timeout: float = DEFAULT_CHECK_TIMEOUT,
    ssl_context: Optional[ssl.SSLContext] = None,
    proxy_factory: Optional[Callable[[], Any]] = None,
    validate_bolt11: Optional[Bolt11Validator] = None,
    register_payer: Optional[Callable[[str], Any]] = None,
    unregister_payer: Optional[Callable[[str], Any]] = None,
) -> CheckResult:
    """Self-test ``noffer_str`` end to end and classify the outcome.

    ``register_payer``/``unregister_payer`` receive the throwaway payer pubkey
    (hex) around the attempt, so the local service can recognise and unwind the
    self-test (see :meth:`ClinkServer.register_selftest_payer`).
    """
    try:
        noffer = noffer_decode(noffer_str)
    except Exception as e:
        return CheckResult(noffer_str, CheckStatus.INVALID_NOFFER, detail=str(e)[:120])

    payer_sk = PrivateKey()
    payer_pub = payer_sk.public_key.hex()

    request_payload = {"offer": noffer.offer, "amount_sats": amount_sat}
    content = nip44.encrypt_to(
        payer_sk.raw_secret, noffer.pubkey, json.dumps(request_payload))

    manager = aionostr.Manager(
        relays=[noffer.relay],
        private_key=payer_sk.hex(),
        ssl_context=ssl_context,
        proxy=proxy_factory() if proxy_factory else None,
    )

    async def publish() -> str:
        return await aionostr._add_event(
            manager,
            kind=CLINK_EVENT_KIND,
            tags=[["p", noffer.pubkey], ["clink_version", CLINK_VERSION]],
            content=content,
            private_key=payer_sk.hex(),
        )

    if register_payer is not None:
        register_payer(payer_pub)
    try:
        return await _run_check(
            noffer, noffer_str, manager,
            payer_sk=payer_sk,
            publish=publish,
            amount_sat=amount_sat,
            validate_bolt11=validate_bolt11,
            timeout=timeout,
        )
    finally:
        if unregister_payer is not None:
            unregister_payer(payer_pub)
