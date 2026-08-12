"""The CLINK plugin runtime: a Nostr service that answers noffer requests.

Mirrors the proven structure of Electrum's bundled NWC plugin (taskgroup-owned
relay manager with a reconnect loop), but speaks the CLINK offers protocol:
subscribe for kind-21001 requests addressed to us, NIP-44-decrypt them, and reply
with a fresh BOLT-11 invoice — gated and liquidity-locked by our own modules.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
import time
from collections import OrderedDict, deque
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set

import electrum_aionostr as aionostr
from electrum_aionostr.event import Event as nEvent
from electrum_aionostr.key import PrivateKey

from electrum.logging import Logger
from electrum.plugin import BasePlugin, hook
from electrum.invoices import PR_PAID, Invoice, Request
from electrum.lnutil import RECEIVED
from electrum.lnurl import (
    LNURL6Data,
    LNURLError,
    callback_lnurl,
    decode_lnurl,
    lightning_address_to_url,
    request_lnurl,
)
from electrum.util import (
    EventListener,
    OldTaskGroup,
    UserFacingException,
    ca_path,
    event_listener,
    get_asyncio_loop,
    get_running_loop,
    log_exceptions,
    make_aiohttp_proxy_connector,
)

from . import nip44, protocol
from .devfee import MIN_PAYOUT_SAT, DevFeeLedger
from .liquidity import LiquidityReserver, receivable_capacity_sat
from .noffer import Noffer, OfferPriceType, noffer_encode
from .offers import Offer, OfferStore, advertised_relay, listen_relays
from .receipts import RETRY_INTERVAL_SEC, ReceiptRegistry, ReceiptTarget
from .relay_probe import (
    ProbeResult,
    RelaySelection,
    normalize_relay_url,
    probe_relay_payable,
    select_payable_relay,
)
from .selftest import CheckResult, CheckStatus, check_noffer

if TYPE_CHECKING:
    from electrum.simple_config import SimpleConfig
    from electrum.wallet import Abstract_Wallet

CLINK_EVENT_KIND = 21001
CLINK_VERSION = "1"
# Ignore requests older than this; the payer has almost certainly timed out.
MAX_REQUEST_AGE_SEC = 60
# Cap on remembered request-event ids (replay guard) to bound memory.
SEEN_EVENTS_MAX = 4096
# How long a payable-relay selection is trusted before it is re-probed. The
# user asked for a cap of 24h; a probe runs at offer creation when this lapses.
RELAY_CACHE_TTL_SEC = 24 * 60 * 60
# Self-test payer identities expire after this long even if the checker never
# unregistered them (crash safety) — comfortably above the check timeout.
SELFTEST_PAYER_TTL_SEC = 120


class ClinkServer(Logger, EventListener):
    """Owns the relay connection and request-handling loop for one wallet."""

    def __init__(self, config: "SimpleConfig", wallet: "Abstract_Wallet", plugin: "ClinkPlugin"):
        Logger.__init__(self)
        self.config = config
        self.wallet = wallet
        self.plugin = plugin
        self.do_stop = False
        self.manager: Optional[aionostr.Manager] = None
        # The relay set the current manager was built with; compared against
        # listen_relay_urls() to detect when it must be rebuilt.
        self._manager_relays: Set[str] = set()
        self.taskgroup: Optional[OldTaskGroup] = None
        self.ssl_context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=ca_path)
        self._seen_events: "OrderedDict[str, None]" = OrderedDict()
        # Recent handled requests, newest last — surfaced in the Qt tab.
        self.recent_activity: "deque[Dict[str, Any]]" = deque(maxlen=50)

        # Stable per-wallet Nostr identity, derived from the (seed-derived) LN
        # node key so the same noffers survive restarts without extra storage.
        self.private_key = self._derive_identity(wallet)
        self.pubkey_hex: str = self.private_key.public_key.hex()

        storage = plugin.get_storage(wallet)
        self.offers = OfferStore(storage, now_fn=time.time)
        self.reserver = LiquidityReserver(
            capacity_fn=lambda: receivable_capacity_sat(self.wallet.lnworker),
            clock_fn=time.time,
        )
        self.devfee = DevFeeLedger(
            storage,
            clock_fn=time.time,
            enabled_fn=lambda: bool(self.config.CLINK_DEVFEE_ENABLED),  # type: ignore[attr-defined]
            rate_fn=lambda: float(self.config.CLINK_DEVFEE_RATE_PERCENT),  # type: ignore[attr-defined]
        )
        # Receipts owed to payers once their invoices settle. Persisted so a
        # receipt survives relay drops / restarts between payment and delivery.
        self.receipts = ReceiptRegistry(storage, clock_fn=time.time)
        # Serialise payout attempts so a post-payment trigger can't race the
        # startup check into two concurrent sends.
        self._devfee_lock = asyncio.Lock()
        # Cached payable-relay selection (see pick_payable_relay). None until the
        # first probe; re-probed once older than RELAY_CACHE_TTL_SEC.
        self._relay_selection: Optional[RelaySelection] = None
        self._relay_selection_at: float = 0.0
        self._relay_pick_lock = asyncio.Lock()
        # Throwaway payer pubkeys of in-flight noffer self-tests (pubkey ->
        # expiry). Requests from these are answered normally but their side
        # effects are unwound — see _issue_invoice. Session-only by design.
        self._selftest_payers: Dict[str, float] = {}
        # Latest self-test results per offer id, plus the in-flight run (the Qt
        # tab polls these; a second "Check noffers" click awaits the same run).
        self.check_results: Dict[str, CheckResult] = {}
        self._check_task: Optional["asyncio.Task[Dict[str, CheckResult]]"] = None
        self.register_callbacks()

    @staticmethod
    def _derive_identity(wallet: "Abstract_Wallet") -> PrivateKey:
        material = b"clink-nostr-identity-v1:" + wallet.lnworker.node_keypair.privkey
        return PrivateKey(raw_secret=hashlib.sha256(material).digest())

    # --- config helpers --------------------------------------------------

    def candidate_relays(self) -> List[str]:
        """Relays to consider for a noffer, in preference order.

        An explicit ``CLINK_RELAY`` override wins outright (the rig injects its
        local relay this way); otherwise fall back to Electrum's validated global
        ``NOSTR_RELAYS`` list, which the payability probe filters down to one that
        actually works.
        """
        explicit = (self.config.CLINK_RELAY or "").strip()  # type: ignore[attr-defined]
        if explicit:
            return [explicit]
        return list(self.config.get_nostr_relays())

    @property
    def relay_url(self) -> str:
        """First-choice relay before any payability probe (the raw fallback)."""
        cands = self.candidate_relays()
        return cands[0].strip() if cands else ""

    @property
    def effective_relay(self) -> str:
        """The relay to advertise/listen on: the probed pick while it is fresh,
        otherwise the raw first choice."""
        sel = self._relay_selection
        if sel and sel.relay and (time.time() - self._relay_selection_at) < RELAY_CACHE_TTL_SEC:
            return sel.relay
        return self.relay_url

    @property
    def invoice_expiry_sec(self) -> int:
        return int(self.config.CLINK_INVOICE_EXPIRY)  # type: ignore[attr-defined]

    def offer_relay(self, offer: Optional[Offer]) -> str:
        """The relay ``offer``'s noffer advertises (custom override or auto pick)."""
        return advertised_relay(offer, self.effective_relay)

    def listen_relay_urls(self) -> List[str]:
        """Every relay the listener must sit on: the auto-selected relay plus
        each distinct custom relay stored on an offer — an offer is payable only
        if we are subscribed on the relay its noffer advertises."""
        return listen_relays(self.offers.list(), self.effective_relay)

    def make_noffer(self, offer_id: str) -> str:
        """Build the noffer string a payer scans for ``offer_id``."""
        return noffer_encode(Noffer(
            pubkey=self.pubkey_hex,
            relay=self.offer_relay(self.offers.get(offer_id)),
            offer=offer_id,
            price_type=OfferPriceType.SPONTANEOUS,
        ))

    async def probe_custom_relay(self, relay: str) -> ProbeResult:
        """Payability-probe a single user-chosen relay (same round-trip check
        the automatic selection uses)."""
        return await probe_relay_payable(
            relay, ssl_context=self.ssl_context, proxy_factory=self._proxy_factory())

    def _proxy_factory(self) -> Optional[Callable[[], Any]]:
        """A factory that mints a fresh aiohttp proxy connector, or None.

        Returns a factory (not a connector) because a connector is single-use per
        session and each relay manager needs its own.
        """
        network = self.wallet.lnworker.network if self.wallet.lnworker else None
        if network and network.proxy and network.proxy.enabled:
            return lambda: make_aiohttp_proxy_connector(network.proxy, self.ssl_context)
        return None

    async def pick_payable_relay(self, *, force: bool = False) -> RelaySelection:
        """Return a payable relay, probing the candidates if the cache is stale.

        The result is cached for ``RELAY_CACHE_TTL_SEC`` (24h). When the chosen
        relay changes, the listener is restarted so the receiver waits on the same
        relay every fresh noffer will advertise — the two must agree or the offer
        is unpayable.
        """
        async with self._relay_pick_lock:
            now = time.time()
            cached = self._relay_selection
            if (not force and cached is not None
                    and (now - self._relay_selection_at) < RELAY_CACHE_TTL_SEC):
                return cached

            factory = self._proxy_factory()

            async def _probe(relay: str) -> ProbeResult:
                return await probe_relay_payable(
                    relay, ssl_context=self.ssl_context, proxy_factory=factory)

            prev_relay = self.effective_relay
            sel = await select_payable_relay(self.candidate_relays(), probe=_probe)
            self._relay_selection = sel
            self._relay_selection_at = now
            if not sel.ok:
                self.logger.warning(
                    "No configured Nostr relay accepted a CLINK test request; "
                    "offers may be unpayable. Tried: "
                    + ", ".join(r.relay for r in sel.results))
            if sel.relay and sel.relay != prev_relay:
                self.logger.info(f"CLINK relay selected: {sel.relay}")
                self.restart_event_handler()
            return sel

    # --- noffer self-test ("Check noffers") --------------------------------

    def register_selftest_payer(self, pubkey_hex: str) -> None:
        """Mark ``pubkey_hex`` as an in-flight self-test payer identity.

        Requests from it are answered through the normal path but their side
        effects are unwound (see ``_issue_invoice``). Entries expire on their
        own so a crashed check can never leave a permanent bypass behind.
        """
        self._selftest_payers[pubkey_hex] = time.time() + SELFTEST_PAYER_TTL_SEC

    def unregister_selftest_payer(self, pubkey_hex: str) -> None:
        self._selftest_payers.pop(pubkey_hex, None)

    def is_selftest_payer(self, pubkey_hex: str) -> bool:
        now = time.time()
        expired = [pk for pk, exp in self._selftest_payers.items() if exp <= now]
        for pk in expired:
            del self._selftest_payers[pk]
        return pubkey_hex in self._selftest_payers

    def _validate_selftest_bolt11(self, bolt11: str, amount_sat: int) -> Optional[str]:
        """Bolt11 validator injected into :func:`clink.selftest.check_noffer`.

        Returns ``None`` when the invoice parses and carries the requested
        amount, else a short problem description for the check result.
        """
        try:
            invoice = Invoice.from_bech32(bolt11)
        except Exception as e:
            return f"invoice does not parse: {str(e)[:80]}"
        got = invoice.get_amount_sat()
        if got != amount_sat:
            return f"invoice amount {got} sat != requested {amount_sat} sat"
        return None

    @property
    def check_running(self) -> bool:
        task = self._check_task
        return task is not None and not task.done()

    async def check_offers(self, offer_ids: Optional[List[str]] = None) -> Dict[str, CheckResult]:
        """Round-trip self-test every listed offer's noffer; return per-offer results.

        Results are also cached in ``self.check_results`` (session-only) for the
        Qt tab. Concurrent calls coalesce onto the in-flight run instead of
        publishing a second batch of requests.
        """
        if self._check_task is not None and not self._check_task.done():
            return await asyncio.shield(self._check_task)
        task = asyncio.ensure_future(self._run_offer_checks(offer_ids))
        self._check_task = task
        try:
            return await task
        finally:
            if self._check_task is task:
                self._check_task = None

    async def _run_offer_checks(self, offer_ids: Optional[List[str]]) -> Dict[str, CheckResult]:
        offers = [o for o in self.offers.list()
                  if offer_ids is None or o.offer_id in offer_ids]
        factory = self._proxy_factory()
        results: Dict[str, CheckResult] = {}

        async def check_one(offer) -> None:
            # Test exactly the noffer string the table / QR shows for this offer.
            noffer_str = self.make_noffer(offer.offer_id)
            result = await check_noffer(
                noffer_str,
                ssl_context=self.ssl_context,
                proxy_factory=factory,
                validate_bolt11=self._validate_selftest_bolt11,
                register_payer=self.register_selftest_payer,
                unregister_payer=self.unregister_selftest_payer,
            )
            result.checked_at = time.time()
            results[offer.offer_id] = result
            self.check_results[offer.offer_id] = result

        await asyncio.gather(*(check_one(o) for o in offers))
        # Drop cached results for offers that were removed meanwhile.
        live = {o.offer_id for o in self.offers.list()}
        for offer_id in [k for k in self.check_results if k not in live]:
            del self.check_results[offer_id]
        return results

    # --- relay lifecycle (mirrors NWC) -----------------------------------

    def get_relay_manager(self) -> aionostr.Manager:
        assert get_asyncio_loop() == get_running_loop(), "ClinkServer must run in the aio event loop"
        nostr_logger = self.logger.getChild("aionostr")
        factory = self._proxy_factory()
        relays = self.listen_relay_urls()
        self._manager_relays = set(relays)
        return aionostr.Manager(
            relays=relays,
            private_key=self.private_key.hex(),
            log=nostr_logger,
            ssl_context=self.ssl_context,
            proxy=factory() if factory else None,
        )

    @log_exceptions
    async def run(self) -> None:
        while True:
            while (not self.relay_url
                       or not self.wallet.network
                       or not self.wallet.network.is_connected()
                       or not self.wallet.lnworker):
                if self.do_stop:
                    return
                await asyncio.sleep(5)
            if not await self.refresh_manager():
                await asyncio.sleep(30)
                continue
            try:
                async with OldTaskGroup() as tg:
                    self.taskgroup = tg
                    await tg.spawn(self.handle_requests())
                    await tg.spawn(self._devfee_startup_check())
                    await tg.spawn(self._redeliver_receipts())
            except asyncio.CancelledError:
                if self.do_stop:
                    return
                self.logger.debug("Restarting clink event handler")
            except Exception as e:
                self.logger.exception(f"Restarting clink event handler after exception: {e}")
                if self.manager:
                    await self.manager.close()
                    self.manager = None
                await asyncio.sleep(30)
            finally:
                self.taskgroup = None

    async def refresh_manager(self) -> bool:
        # Rebuild the manager whenever the desired relay set drifted from the
        # one it was built with (relay re-pick, or an offer added/removed a
        # custom relay) — a live manager never re-reads its relay list.
        if self.manager is not None and self._manager_relays != set(self.listen_relay_urls()):
            await self.manager.close()
            self.manager = None
        if self.manager is None:
            self.manager = self.get_relay_manager()
        if len(self.manager.relays) <= 0:
            await self.manager.close()
            self.manager = self.get_relay_manager()
        if not self.manager.connected:
            await self.manager.connect()
        if len(self.manager.relays) <= 0:
            self.logger.warning("Could not connect to any relays!")
            return False
        return True

    def restart_event_handler(self) -> None:
        if tg := self.taskgroup:
            asyncio.run_coroutine_threadsafe(tg.cancel_remaining(), get_asyncio_loop())

    # --- request handling ------------------------------------------------

    async def handle_requests(self) -> None:
        query = {
            "kinds": [CLINK_EVENT_KIND],
            "#p": [self.pubkey_hex],
            "since": int(time.time()),
            "limit": 0,
        }
        self.logger.info(f"listening for offers on {', '.join(self.listen_relay_urls())} as {self.pubkey_hex}")
        async for event in self.manager.get_events(query, single_event=False, only_stored=False):
            try:
                await self._dispatch(event)
            except Exception:
                self.logger.exception("error handling clink request")

    def _already_seen(self, event_id: str) -> bool:
        if event_id in self._seen_events:
            return True
        self._seen_events[event_id] = None
        while len(self._seen_events) > SEEN_EVENTS_MAX:
            self._seen_events.popitem(last=False)
        return False

    async def _dispatch(self, event: nEvent) -> None:
        if event.kind != CLINK_EVENT_KIND:
            return
        # Skip our own responses (kind is shared by request and response).
        if event.pubkey == self.pubkey_hex:
            return
        if self._already_seen(event.id):
            return
        if event.expires_at() is not None:
            if event.is_expired():
                return
        elif event.created_at < int(time.time()) - MAX_REQUEST_AGE_SEC:
            return

        try:
            plaintext = nip44.decrypt_from(self.private_key.raw_secret, event.pubkey, event.content)
            req = json.loads(plaintext)
            if not isinstance(req, dict):
                raise ValueError("request is not a JSON object")
        except Exception:
            self.logger.debug("could not decrypt/parse clink request", exc_info=True)
            return

        selftest = self.is_selftest_payer(event.pubkey)
        offer = self.offers.get(req.get("offer", ""))
        resolution = protocol.resolve_request(req, offer, self.reserver.available_sat())
        if isinstance(resolution, protocol.SendError):
            self._record(req.get("offer", ""), protocol.request_amount_sat(req),
                         f"error {resolution.payload.get('code')}"
                         + (" (self-test)" if selftest else ""))
            await self.send_response(event, resolution.payload)
            return

        # Fold in the payer's requested memo only when this offer permits it;
        # otherwise the invoice carries just the merchant's label.
        description = protocol.effective_description(offer, req)
        await self._issue_invoice(
            event, offer, resolution.amount_sat, description, selftest=selftest)

    def _record(self, offer_id: str, amount: Optional[int], result: str) -> None:
        self.recent_activity.append({
            "time": int(time.time()), "offer": offer_id,
            "amount_sat": amount, "result": result,
        })

    async def _issue_invoice(self, event: nEvent, offer, amount_sat: int,
                             description: Optional[str] = None, *,
                             selftest: bool = False) -> None:
        expiry = self.invoice_expiry_sec
        # Honor the payer's requested memo (NIP-69 description), combined with
        # the merchant's offer label, so the invoice carries who-it's-for context
        # (e.g. cashupayserver sends the store name). Capped/sanitized upstream.
        message = protocol.invoice_message(offer.label if offer else None, description)
        try:
            key = self.wallet.create_request(
                amount_sat=amount_sat, message=message, exp_delay=expiry, address=None)
            request: Request = self.wallet.get_request(key)
            info = self.wallet.lnworker.get_payment_info(request.payment_hash, direction=RECEIVED)
            _, bolt11 = self.wallet.lnworker.get_bolt11_invoice(
                payment_info=info, message=message, fallback_address=None)
        except Exception as e:
            self.logger.exception("failed to create invoice")
            await self.send_response(
                event, protocol.error_payload(protocol.ERR_TEMPORARY_FAILURE, f"Temporary Failure: {str(e)[:80]}"))
            return

        # Atomically lock the inbound liquidity for this invoice's lifetime.
        # try_reserve re-checks under lock, so a request that lost a concurrent
        # race here is cancelled rather than overcommitting capacity.
        reserved = self.reserver.try_reserve(request.rhash, amount_sat, expires_at=time.time() + expiry)
        if not reserved:
            self.wallet.delete_request(key)
            self._record(req_offer_id(offer), amount_sat, "error 5 (lost race)")
            await self.send_response(
                event, protocol.invalid_amount_payload(1, self.reserver.available_sat()))
            return

        if not selftest:
            # Remember this hash so the dev fee accrues if (and only if) it is paid.
            self.devfee.mark_issued(request.rhash)
            # Remember who to send the payment receipt to once this invoice settles.
            self.receipts.remember(request.rhash, event.pubkey, event.id,
                                   expires_at=time.time() + expiry)
            self.logger.info(f"issued {amount_sat} sat invoice for offer {req_offer_id(offer)} "
                             f"(rhash={request.rhash[:10]}…), liquidity locked for {expiry}s")
            self._record(req_offer_id(offer), amount_sat, "invoice issued")
        await self.send_response(event, protocol.success_payload(bolt11))
        if selftest:
            # A real invoice went over the wire (that is what the check proves),
            # but nobody will ever pay it: unwind the side effects right away so
            # a self-test never holds liquidity or leaves a request behind.
            self.reserver.release(request.rhash)
            self.wallet.delete_request(key)
            self.logger.info(f"answered noffer self-test for offer {req_offer_id(offer)}")
            self._record(req_offer_id(offer), amount_sat, "self-test ✓")

    def _encrypt_event_args(self, to_pubkey: str, request_event_id: str,
                            payload: Dict[str, Any]) -> Dict[str, Any]:
        """Build the kind-21001 event kwargs addressed to ``to_pubkey``.

        The ``["e", request_event_id]`` tag is what lets the payer's open
        subscription (filtered on ``#p`` + ``#e``) match both the invoice and the
        later receipt for the same request.
        """
        content = nip44.encrypt_to(self.private_key.raw_secret, to_pubkey, json.dumps(payload))
        tags = [["p", to_pubkey], ["e", request_event_id], ["clink_version", CLINK_VERSION]]
        return dict(kind=CLINK_EVENT_KIND, tags=tags, content=content,
                    private_key=self.private_key.hex())

    async def send_response(self, request_event: nEvent, payload: Dict[str, Any]) -> None:
        tg = self.taskgroup
        if tg is None:
            return
        await tg.spawn(aionostr._add_event(
            self.manager,
            **self._encrypt_event_args(request_event.pubkey, request_event.id, payload),
        ))

    # --- payment receipts ------------------------------------------------

    async def _deliver_receipt(self, target: ReceiptTarget) -> bool:
        """Publish the ``{"res":"ok"}`` receipt for a settled invoice.

        Best-effort and idempotent: stamps the attempt first (so a failure waits
        a full retry interval), awaits the relay publish, and only on success
        removes the owed entry. Never raises — a failure leaves the receipt owed
        for the periodic retry loop.
        """
        self.receipts.record_attempt(target.rhash)
        if self.manager is None:
            return False
        try:
            await asyncio.wait_for(aionostr._add_event(
                self.manager,
                **self._encrypt_event_args(
                    target.payer_pubkey, target.request_event_id, protocol.receipt_payload()),
            ), timeout=30)
        except Exception as e:
            self.logger.warning(
                f"receipt delivery failed for {target.rhash[:10]}… "
                f"(attempt {target.attempts + 1}); will retry: {e!r}")
            return False
        self.receipts.mark_sent(target.rhash)
        self.logger.info(f"receipt delivered to {target.payer_pubkey[:10]}… "
                         f"for {target.rhash[:10]}…")
        self._record("receipt", None, "receipt sent ✓")
        return True

    async def _redeliver_receipts(self) -> None:
        """Retry any owed receipts now and hourly thereafter.

        Runs inside the relay taskgroup, so it also fires once on every
        reconnect/restart — covering receipts owed while we were offline.
        """
        while True:
            try:
                self.receipts.sweep()
                for target in self.receipts.due_targets():
                    await self._deliver_receipt(target)
            except Exception:
                self.logger.exception("error redelivering receipts")
            await asyncio.sleep(RETRY_INTERVAL_SEC)

    @event_listener
    def on_event_request_status(self, wallet, key, status):
        # Once an invoice is paid the real receivable capacity drops, so release
        # its soft reservation immediately to avoid double-counting.
        if wallet != self.wallet or status != PR_PAID:
            return
        request = self.wallet.get_request(key)
        if not (request and request.is_lightning()):
            return
        self.reserver.release(request.rhash)
        # A receipt is now owed to the payer of this CLINK invoice; persist that
        # (mark_due) and fire a best-effort delivery on the asyncio loop. The
        # entry stays owed until the relay accepts it, so a drop here is retried.
        target = self.receipts.mark_due(request.rhash)
        if target is not None:
            asyncio.run_coroutine_threadsafe(
                self._deliver_receipt(target), get_asyncio_loop())
        # Accrue the dev fee on payments to invoices we issued for a CLINK offer.
        if self.devfee.take_issued(request.rhash):
            amount_sat = request.get_amount_sat()
            if isinstance(amount_sat, int) and amount_sat > 0:
                added = self.devfee.accrue(amount_sat)
                if added:
                    self.logger.info(
                        f"dev fee +{added} msat on {amount_sat} sat payment "
                        f"(owed now {self.devfee.owed_sat()} sat)")
            # A payment just arrived; see whether a payout is now due.
            self._schedule_devfee_payout()

    # --- dev-fee payout --------------------------------------------------

    @property
    def devfee_dest(self) -> str:
        return (self.config.CLINK_DEVFEE_DEST or "").strip()  # type: ignore[attr-defined]

    def _schedule_devfee_payout(self, *, force: bool = False) -> None:
        """Fire-and-forget a payout attempt on the asyncio loop."""
        asyncio.run_coroutine_threadsafe(
            self.maybe_pay_devfee(force=force), get_asyncio_loop())

    async def _devfee_startup_check(self) -> None:
        """Once the wallet is online after launch, try any payout left owing."""
        while not (self.wallet.network and self.wallet.network.is_connected()
                   and self.wallet.lnworker):
            if self.do_stop:
                return
            await asyncio.sleep(5)
        await self.maybe_pay_devfee()

    async def _resolve_devfee_lnurl(self, dest: str) -> LNURL6Data:
        """Resolve a Lightning address, LNURL, or direct URL into a pay descriptor."""
        if dest.startswith("http://") or dest.startswith("https://"):
            url = dest
        elif "@" in dest:
            url = lightning_address_to_url(dest)
            if not url:
                raise LNURLError(f"invalid Lightning address: {dest}")
        else:
            url = decode_lnurl(dest)
        data = await request_lnurl(url)
        if not isinstance(data, LNURL6Data):
            raise LNURLError(f"dev-fee destination is not an LNURL-pay endpoint: {dest}")
        return data

    async def maybe_pay_devfee(self, *, force: bool = False) -> Dict[str, Any]:
        """Forward the accrued dev fee if a payout is due.

        Returns a small status dict (``{"paid": bool, "reason"/"amount_sat"}``).
        Never raises: a failed payout is recorded and simply retried next window.
        """
        async with self._devfee_lock:
            if not self.devfee.should_attempt(ignore_interval=force):
                return {"paid": False, "reason": "not due",
                        "owed_sat": self.devfee.owed_sat()}
            dest = self.devfee_dest
            if not dest:
                return {"paid": False, "reason": "no destination configured"}

            # Never more than is owed (payable_sat, already >= MIN_PAYOUT_SAT and
            # capped at the 24h limit); clamp down to the endpoint's max.
            payable = self.devfee.payable_sat()
            try:
                lnurl = await self._resolve_devfee_lnurl(dest)
                if lnurl.min_sendable_sat > payable or lnurl.max_sendable_sat < MIN_PAYOUT_SAT:
                    # The endpoint can't accept a payment in our range; don't burn
                    # the daily attempt over a sizing mismatch — retry later.
                    return {"paid": False, "reason": "amount outside LNURL range"}
                amount_sat = min(payable, lnurl.max_sendable_sat)
                bolt11 = await self._request_devfee_invoice(lnurl, amount_sat)
            except Exception as e:
                self.logger.warning(f"dev-fee payout could not be prepared: {e!r}")
                self.devfee.record_failure()
                return {"paid": False, "reason": f"prepare failed: {e}"}

            try:
                invoice = Invoice.from_bech32(bolt11)
                if invoice.get_amount_sat() != amount_sat:
                    raise LNURLError("LNURL returned an invoice with the wrong amount")
                self.logger.info(f"paying {amount_sat} sat dev fee to {dest}")
                success, log = await self.wallet.lnworker.pay_invoice(invoice)
            except Exception as e:
                self.logger.warning(f"dev-fee payout failed: {e!r}")
                self.devfee.record_failure()
                self._record("devfee", amount_sat, f"dev-fee payment failed")
                return {"paid": False, "reason": f"payment failed: {e}"}

            if not success:
                self.devfee.record_failure()
                self._record("devfee", amount_sat, "dev-fee payment failed")
                return {"paid": False, "reason": "payment did not complete"}

            self.devfee.record_success(amount_sat)
            self.logger.info(f"dev fee paid: {amount_sat} sat to {dest} "
                             f"(owed now {self.devfee.owed_sat()} sat)")
            self._record("devfee", amount_sat, "dev-fee paid 💜")
            return {"paid": True, "amount_sat": amount_sat,
                    "owed_sat": self.devfee.owed_sat()}

    async def _request_devfee_invoice(self, lnurl: LNURL6Data, amount_sat: int) -> str:
        params: Dict[str, Any] = {"amount": amount_sat * 1000}
        if lnurl.comment_allowed:
            params["comment"] = "CLINK dev fee — thanks! 💜"[: lnurl.comment_allowed]
        response = await callback_lnurl(lnurl.callback_url, params=params)
        bolt11 = response.get("pr")
        if not bolt11:
            raise LNURLError("LNURL pay response did not include an invoice")
        return bolt11


def req_offer_id(offer) -> str:
    return offer.offer_id if offer else "?"


class ClinkPlugin(BasePlugin):
    """Electrum plugin entry point: wires the server to wallet lifecycle."""

    def __init__(self, parent, config: "SimpleConfig", name):
        BasePlugin.__init__(self, parent, config, name)
        self.config = config
        self.server: Optional[ClinkServer] = None
        self.taskgroup = OldTaskGroup()
        self.initialized = False

    def start_plugin(self, wallet: "Abstract_Wallet"):
        if not wallet.has_lightning():
            self.logger.info("wallet has no lightning; CLINK offers need it to issue invoices")
            return
        if self.initialized:
            return  # only drive a single wallet
        self.server = ClinkServer(self.config, wallet, self)
        asyncio.run_coroutine_threadsafe(
            self.taskgroup.spawn(self.server.run()), get_asyncio_loop())
        self.initialized = True
        self.logger.info("CLINK plugin started")

    @hook
    def close_wallet(self, *args, **kwargs):
        async def close():
            if self.server:
                self.server.do_stop = True
                self.server.unregister_callbacks()
                if self.server.manager:
                    await self.server.manager.close()
            await self.taskgroup.cancel_remaining()
        asyncio.run_coroutine_threadsafe(close(), get_asyncio_loop())

    # --- API used by cmdline + Qt ----------------------------------------

    async def create_offer(self, label: str = "", allow_payer_memo: bool = True,
                           relay: str = "") -> Dict[str, Any]:
        """Create an offer; ``relay`` (``wss://…``) pins a custom relay instead
        of the automatic pick. A custom relay is payability-probed first and a
        failure *blocks* creation (the user chose it explicitly, so a dead or
        refusing relay is surfaced as an error, not a warning)."""
        assert self.server is not None, "wallet not loaded yet"
        custom_relay = (relay or "").strip()
        if custom_relay:
            return await self._create_offer_custom_relay(
                custom_relay, label=label, allow_payer_memo=allow_payer_memo)
        # Probe (cached 24h) before building the noffer so its single embedded
        # relay is one a payer can actually reach — see clink.relay_probe.
        selection = await self.server.pick_payable_relay()
        offer = self.server.offers.create(label=label, allow_payer_memo=allow_payer_memo)
        result: Dict[str, Any] = {
            "offer_id": offer.offer_id, "label": offer.label,
            "allow_payer_memo": offer.allow_payer_memo,
            "noffer": self.server.make_noffer(offer.offer_id),
            "relay": selection.relay,
            "relay_payable": selection.ok,
        }
        if not selection.ok:
            tried = ", ".join(r.relay for r in selection.results) or "(none configured)"
            result["warning"] = (
                "No Nostr relay accepted a test payment request, so this offer "
                f"may not be payable. Tried: {tried}. Set a working relay in the "
                "CLINK settings and create the offer again.")
        return result

    async def _create_offer_custom_relay(self, relay: str, *, label: str,
                                         allow_payer_memo: bool) -> Dict[str, Any]:
        assert self.server is not None
        relay = normalize_relay_url(relay)  # ValueError on a malformed URL
        probe = await self.server.probe_custom_relay(relay)
        if not probe.ok:
            detail = f": {probe.detail}" if probe.detail else ""
            raise UserFacingException(
                f"Relay {relay} failed the payability check "
                f"({probe.status.value}{detail}). The offer was not created.")
        # The listener must sit on this relay too, or requests for the offer
        # would never arrive; restart it only when the relay is actually new.
        needs_listener = relay not in self.server.listen_relay_urls()
        offer = self.server.offers.create(
            label=label, allow_payer_memo=allow_payer_memo, relay=relay)
        if needs_listener:
            self.server.restart_event_handler()
        return {
            "offer_id": offer.offer_id, "label": offer.label,
            "allow_payer_memo": offer.allow_payer_memo,
            "noffer": self.server.make_noffer(offer.offer_id),
            "relay": relay,
            "relay_payable": True,
        }

    def list_offers(self) -> Dict[str, Any]:
        # Read-only status getters tolerate a missing server (wallet not yet
        # loaded, or torn down on shutdown while the Qt poller is still firing).
        if self.server is None:
            return {}
        return {o.offer_id: {"label": o.label, "active": o.active,
                             "allow_payer_memo": o.allow_payer_memo,
                             "noffer": self.server.make_noffer(o.offer_id),
                             "relay": self.server.offer_relay(o),
                             "relay_custom": bool(o.relay)}
                for o in self.server.offers.list()}

    def candidate_relay_urls(self) -> List[str]:
        """Relays offered in the Qt "New offer" relay dropdown (config order)."""
        if self.server is None:
            return []
        return self.server.candidate_relays()

    def set_offer_label(self, offer_id: str, label: str) -> bool:
        assert self.server is not None, "wallet not loaded yet"
        return self.server.offers.set_label(offer_id, label)

    def set_offer_allow_payer_memo(self, offer_id: str, allow: bool) -> bool:
        assert self.server is not None, "wallet not loaded yet"
        return self.server.offers.set_allow_payer_memo(offer_id, allow)

    def remove_offer(self, offer_id: str) -> bool:
        assert self.server is not None, "wallet not loaded yet"
        before = set(self.server.listen_relay_urls())
        removed = self.server.offers.remove(offer_id)
        # Drop the listener connection to a custom relay no other offer needs.
        if removed and set(self.server.listen_relay_urls()) != before:
            self.server.restart_event_handler()
        return removed

    async def check_noffers(self, offer_id: Optional[str] = None) -> Dict[str, Any]:
        """Self-test the noffer of one offer (or all offers) end to end.

        Returns ``{offer_id: result_dict}`` — see :class:`clink.selftest.CheckResult`.
        """
        assert self.server is not None, "wallet not loaded yet"
        results = await self.server.check_offers([offer_id] if offer_id else None)
        return {oid: res.to_dict() for oid, res in results.items()}

    def noffer_check_results(self) -> Dict[str, "CheckResult"]:
        """Latest session-only self-test results per offer id (may be empty)."""
        if self.server is None:
            return {}
        return dict(self.server.check_results)

    def noffer_check_running(self) -> bool:
        return self.server is not None and self.server.check_running

    def liquidity_status(self) -> Dict[str, Any]:
        if self.server is None:
            return {"available_sat": 0, "reserved_sat": 0, "active_reservations": 0,
                    "owed_receipts": 0}
        return {
            "available_sat": self.server.reserver.available_sat(),
            "reserved_sat": self.server.reserver.reserved_sat(),
            "active_reservations": len(self.server.reserver.active()),
            "owed_receipts": self.server.receipts.owed_count(),
        }

    def recent_activity(self) -> list:
        if self.server is None:
            return []
        return list(self.server.recent_activity)

    def devfee_status(self) -> Dict[str, Any]:
        if self.server is None:
            return {"owed_sat": 0, "destination": ""}
        status = self.server.devfee.status()
        status["destination"] = self.server.devfee_dest
        return status

    async def devfee_pay_now(self) -> Dict[str, Any]:
        assert self.server is not None, "wallet not loaded yet"
        return await self.server.maybe_pay_devfee(force=True)

    @property
    def identity_pubkey(self) -> Optional[str]:
        return self.server.pubkey_hex if self.server else None
