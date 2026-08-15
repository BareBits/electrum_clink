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
from electrum.invoices import PR_EXPIRED, PR_PAID, PR_UNPAID, Invoice, Request
from electrum.lnurl import (
    LNURL6Data,
    LNURLError,
    callback_lnurl,
    decode_lnurl,
    lightning_address_to_url,
    request_lnurl,
)
from electrum.lnutil import RECEIVED
from electrum.logging import Logger
from electrum.plugin import BasePlugin, hook
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
from electrum_aionostr.event import Event as nEvent
from electrum_aionostr.key import PrivateKey

from . import nip44, protocol
from .devfee import MIN_PAYOUT_SAT, DevFeeLedger
from .liquidity import LiquidityReserver, receivable_capacity_sat
from .liveness import LivenessResult, RelayLivenessMonitor
from .metadata import default_offer, merge_clink_offer
from .noffer import Noffer, OfferPriceType, noffer_encode
from .offers import (
    Offer,
    OfferStore,
    advertised_relay,
    advertised_relays,
    listen_relays,
)
from .receipts import RETRY_INTERVAL_SEC, ReceiptRegistry, ReceiptTarget
from .relay_probe import (
    ProbeResult,
    RelaySelection,
    normalize_relay_url,
    probe_relay_payable,
    select_payable_relay,
)
from .selftest import SELFTEST_AMOUNT_SAT, CheckResult, check_noffer

if TYPE_CHECKING:
    from electrum.simple_config import SimpleConfig
    from electrum.wallet import Abstract_Wallet

CLINK_EVENT_KIND = 21001
CLINK_VERSION = "1"
# Ignore requests older than this; the payer has almost certainly timed out.
MAX_REQUEST_AGE_SEC = 60
# Reject requests dated further in the future than this. A future-dated event
# would otherwise look "fresh" (and thus replayable) until its timestamp lapses.
MAX_CLOCK_SKEW_SEC = 60
# Cap on remembered request-event ids (replay guard) to bound memory.
SEEN_EVENTS_MAX = 4096
# Cap on outstanding *unpaid* invoices per payer pubkey. Generous because one
# pubkey may be a merchant frontend fanning out many customers' requests; slots
# free up as invoices are paid or expire (tracked via the receipt registry, so
# the cap survives restarts). Over-cap requests get a retryable error.
MAX_PENDING_PER_PAYER = 200
# How long a payable-relay selection is trusted before it is re-probed. The
# user asked for a cap of 24h; a probe runs at offer creation when this lapses.
RELAY_CACHE_TTL_SEC = 24 * 60 * 60
# Self-test payer identities expire after this long even if the checker never
# unregistered them (crash safety) — comfortably above the check timeout.
SELFTEST_PAYER_TTL_SEC = 120


def _offer_price(price: Optional[int]) -> Optional[int]:
    """Validate a fixed-offer price (sats); return it or raise.

    ``None`` or ``0`` means "spontaneous offer" (the cmdline and Qt surfaces
    both map an absent/zero input to ``None`` anyway). A fixed price must be a
    positive integer within the bitcoin supply; anything else is rejected before
    it can be stored, so a stored FIXED offer is always payable at a known amount.
    """
    if price is None or price == 0:
        return None
    if isinstance(price, bool) or not isinstance(price, int) or price < 1:
        raise UserFacingException(
            "Fixed offer price must be a positive integer amount in sats.")
    if price > protocol.MAX_AMOUNT_SAT:
        raise UserFacingException(
            f"Fixed offer price exceeds the bitcoin supply cap "
            f"({protocol.MAX_AMOUNT_SAT} sats).")
    return price


def _offer_price_type(price: Optional[int]) -> OfferPriceType:
    # Validate through _offer_price so 0/None (spontaneous) and invalid input
    # (raises) are classified identically everywhere.
    return OfferPriceType.FIXED if _offer_price(price) is not None else OfferPriceType.SPONTANEOUS


def _offer_expires_at(expires_in: Optional[int]) -> int:
    """The absolute epoch expiry for an offer with a ``expires_in`` lifetime.

    ``None``/``0`` means "never expires" (``0``). ``expires_in`` must be a
    positive number of seconds; anything else is rejected before it can be
    stored so a stored offer always has a sane ``expires_at``.
    """
    if expires_in is None or expires_in == 0:
        return 0
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in < 1:
        raise UserFacingException(
            "Offer expiry must be a positive number of seconds.")
    return int(time.time()) + expires_in


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
        # Hourly liveness monitor for the relays existing noffers advertise
        # (driven from the receipt-redelivery tick; results are session-only).
        self.liveness = RelayLivenessMonitor(
            probe=self._liveness_probe, clock_fn=time.time)
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
        """The plugin-wide default relay: the probed pick while it is fresh,
        otherwise the raw first choice. Only new offers (at creation) and
        pre-pinning legacy offers derive their relay from this — a created
        offer carries its own pinned relay."""
        sel = self._relay_selection
        if sel and sel.relay and (time.time() - self._relay_selection_at) < RELAY_CACHE_TTL_SEC:
            return sel.relay
        return self.relay_url

    @property
    def invoice_expiry_sec(self) -> int:
        return int(self.config.CLINK_INVOICE_EXPIRY)  # type: ignore[attr-defined]

    @property
    def advertise_metadata(self) -> bool:
        # Opt-out: publish the default offer's noffer in kind-0 metadata so
        # profiles/directories can surface a CLINK payment entry point.
        return bool(self.config.CLINK_ADVERTISE_METADATA)  # type: ignore[attr-defined]

    def offer_relay(self, offer: Optional[Offer]) -> str:
        """The relay ``offer``'s noffer advertises (its pinned relay, or the
        plugin-wide default for a pre-pinning legacy offer)."""
        return advertised_relay(offer, self.effective_relay)

    def listen_relay_urls(self) -> List[str]:
        """Every relay the listener must sit on: each offer's pinned relay,
        plus the plugin-wide default while any offer still needs it — an offer
        is payable only if we are subscribed on the relay its noffer
        advertises."""
        return listen_relays(self.offers.list(), self.effective_relay)

    def advertised_relay_urls(self) -> List[str]:
        """Every distinct relay some existing offer's noffer advertises —
        exactly what the periodic liveness check probes. Empty with no offers."""
        return advertised_relays(self.offers.list(), self.effective_relay)

    def make_noffer(self, offer_id: str) -> str:
        """Build the noffer string a payer scans for ``offer_id``."""
        offer = self.offers.get(offer_id)
        return noffer_encode(Noffer(
            pubkey=self.pubkey_hex,
            relay=self.offer_relay(offer),
            offer=offer_id,
            # Advertise the offer's own pricing so a payer sees the fixed price
            # (TLV 4) and knows the amount is not negotiable before even asking.
            price_type=offer.price_type if offer else OfferPriceType.SPONTANEOUS,
            price=offer.price if offer else None,
        ))

    def _noffer_for(self, offer_id: str) -> Optional[str]:
        """The noffer for ``offer_id`` if it is still payable, else ``None``.

        Injected into the protocol as the ``latest`` resolver for code-3
        responses: a replacement offer only ever surfaces to the payer while it
        is actually usable (exists, active, not yet expired), so ``latest`` can
        never forward a payer to another broken offer.
        """
        offer = self.offers.get(offer_id)
        if offer is None or not offer.active or protocol.offer_expired(offer):
            return None
        return self.make_noffer(offer_id)

    async def _liveness_probe(self, relay: str) -> ProbeResult:
        """Probe callable injected into the liveness monitor (fresh proxy each run)."""
        return await probe_relay_payable(
            relay, ssl_context=self.ssl_context, proxy_factory=self._proxy_factory())

    async def check_relay_liveness(self, *, retry_delay: Optional[float] = None,
                                   ) -> Dict[str, LivenessResult]:
        """Probe every relay an existing noffer advertises; log confirmed failures.

        Runs hourly from the receipt tick and on demand via ``clink_check_relays``.
        Never mutates the relay selection — a down relay is surfaced (log + Qt
        tab) for the user to act on.
        """
        results = await self.liveness.run_once(
            self.advertised_relay_urls(), retry_delay=retry_delay)
        for res in results.values():
            if not res.ok:
                self.logger.warning(
                    f"CLINK relay liveness: {res.relay} failed the payability "
                    f"check twice ({res.status.value}"
                    + (f": {res.detail}" if res.detail else "")
                    + ") — offers advertising it may be unpayable")
        return results

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
                if self._pin_legacy_offer_relays(cached):
                    self.restart_event_handler()
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
            restart = bool(sel.relay) and sel.relay != prev_relay
            if restart:
                self.logger.info(f"CLINK relay selected: {sel.relay}")
            if self._pin_legacy_offer_relays(sel) or restart:
                self.restart_event_handler()
            return sel

    def _pin_legacy_offer_relays(self, sel: RelaySelection) -> bool:
        """Migrate pre-pinning offers onto the probed relay ``sel``.

        Offers stored before relays were pinned at creation have an empty
        ``relay`` and used to silently re-derive it (and thus a *different*
        noffer) from the config order after every restart. The first payability
        probe that succeeds writes its pick onto them, making their noffers
        stable from then on. Returns whether anything was pinned (the listener
        set changed).
        """
        if not sel.ok:
            return False
        pinned = self.offers.pin_missing_relays(sel.relay)
        if pinned:
            self.logger.info(
                f"pinned relay {sel.relay} on {len(pinned)} pre-existing "
                f"offer(s): {', '.join(pinned)}")
        return bool(pinned)

    async def ensure_offer_relays_pinned(self) -> None:
        """Run a relay pick when a pre-pinning offer still lacks its relay.

        Called from :meth:`run` before the listener manager is (re)built, so a
        legacy offer is migrated onto a probed relay — and the listener covers
        it — as soon as the plugin comes up, not only when the user happens to
        create another offer. A no-op once every offer carries a pinned relay.
        """
        if all((o.relay or "").strip() for o in self.offers.list()):
            return
        try:
            await self.pick_payable_relay()
        except Exception:
            self.logger.exception("could not pin a relay on legacy offers")

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
            # A fixed offer is only payable at its price; test it at that amount.
            amount = offer.price if offer.price_type == OfferPriceType.FIXED else None
            result = await check_noffer(
                noffer_str,
                amount_sat=amount if amount is not None else SELFTEST_AMOUNT_SAT,
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
            await self.ensure_offer_relays_pinned()
            if not await self.refresh_manager():
                await asyncio.sleep(30)
                continue
            try:
                async with OldTaskGroup() as tg:
                    self.taskgroup = tg
                    await tg.spawn(self.handle_requests())
                    await tg.spawn(self._devfee_startup_check())
                    await tg.spawn(self._redeliver_receipts())
                    await tg.spawn(self.sync_metadata())
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
        # Freshness is enforced on created_at unconditionally: an expiration tag
        # may only *shrink* the acceptance window, never extend it — otherwise a
        # compromised relay could replay a captured request for the tag's whole
        # lifetime. Future-dated events are rejected for the same reason.
        try:
            if event.is_expired():
                return
        except Exception:
            return  # unparseable expiration tag -> treat as invalid, drop
        now = int(time.time())
        if not (now - MAX_REQUEST_AGE_SEC <= event.created_at <= now + MAX_CLOCK_SKEW_SEC):
            return

        try:
            plaintext = nip44.decrypt_from(self.private_key.raw_secret, event.pubkey, event.content)
            req = json.loads(plaintext)
            if not isinstance(req, dict):
                raise ValueError("request is not a JSON object")
        except Exception:
            self.logger.debug("could not decrypt/parse clink request", exc_info=True)
            return

        # Replay guard, checked only for decryptable requests: junk events
        # (which anyone can mass-produce with throwaway keys) must not be able
        # to evict real entries from the bounded seen-cache and reopen a
        # replay window for a recently answered request.
        if self._already_seen(event.id):
            return

        selftest = self.is_selftest_payer(event.pubkey)
        offer_id = protocol.request_offer_id(req)
        offer = self.offers.get(offer_id) if offer_id is not None else None
        resolution = protocol.resolve_request(
            req, offer, self.reserver.available_sat(), noffer_for=self._noffer_for)
        if isinstance(resolution, protocol.SendError):
            self._record(offer_id or "", protocol.request_amount_sat(req),
                         f"error {resolution.payload.get('code')}"
                         + (" (self-test)" if selftest else ""))
            await self.send_response(event, resolution.payload)
            return

        # Per-payer ceiling on outstanding unpaid invoices: bounds the wallet
        # requests and liquidity one pubkey can hold. Answered with a retryable
        # error so an honest-but-busy merchant frontend recovers as its
        # invoices are paid or expire.
        if not selftest and self.receipts.pending_count_for(event.pubkey) >= MAX_PENDING_PER_PAYER:
            self._record(offer_id or "", resolution.amount_sat, "error 2 (payer request cap)")
            await self.send_response(event, protocol.error_payload(
                protocol.ERR_TEMPORARY_FAILURE, "Temporary Failure"))
            return

        # Fold in the payer's requested memo only when this offer permits it;
        # otherwise the invoice carries just the merchant's label.
        description = protocol.effective_description(offer, req)
        # Honor the payer's requested invoice expiry (clamped by protocol) — the
        # same value gates the bolt11, the liquidity reservation and the
        # receipt registry, so a request can't widen one window behind another's.
        expiry_sec = protocol.effective_expiry_sec(req, self.invoice_expiry_sec)
        await self._issue_invoice(
            event, offer, resolution.amount_sat, description,
            expiry_sec=expiry_sec, selftest=selftest)

    def _record(self, offer_id: str, amount: Optional[int], result: str) -> None:
        # offer_id can originate from a hostile payer; keep the activity feed
        # (and the Qt tab rendering it) bounded per entry.
        self.recent_activity.append({
            "time": int(time.time()), "offer": str(offer_id)[:32],
            "amount_sat": amount, "result": result,
        })

    async def _issue_invoice(self, event: nEvent, offer, amount_sat: int,
                             description: Optional[str] = None, *,
                             expiry_sec: Optional[int] = None,
                             selftest: bool = False) -> None:
        # The caller (dispatch) computes the effective expiry once from the
        # request (payer's clamped expires_in_seconds, else our default) so the
        # bolt11, reservation and receipt-registry windows all agree.
        expiry = int(expiry_sec) if expiry_sec is not None else self.invoice_expiry_sec
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
        except Exception:
            # Deliberately generic: internal exception text (paths, wallet
            # state) must not leak to an anonymous payer. Details are logged.
            self.logger.exception("failed to create invoice")
            await self.send_response(
                event, protocol.error_payload(protocol.ERR_TEMPORARY_FAILURE, "Temporary Failure"))
            return

        # Atomically lock the inbound liquidity for this invoice's lifetime.
        # try_reserve re-checks under lock, so a request that lost a concurrent
        # race here is cancelled rather than overcommitting capacity.
        reserved = self.reserver.try_reserve(request.rhash, amount_sat, expires_at=time.time() + expiry)
        if not reserved:
            self.wallet.delete_request(key)
            self._record(req_offer_id(offer), amount_sat, "error 5 (lost race)")
            await self.send_response(
                event, protocol.invalid_amount_payload(
                    1, protocol.quantize_available_sat(self.reserver.available_sat())))
            return

        if not selftest:
            # Remember this hash so the dev fee accrues if (and only if) it is paid.
            self.devfee.mark_issued(request.rhash)
            # Remember who to send the payment receipt to once this invoice settles.
            evicted = self.receipts.remember(request.rhash, event.pubkey, event.id,
                                             expires_at=time.time() + expiry)
            # An entry evicted by the registry cap must not orphan its wallet
            # request — drop it (if still unpaid) along with the entry.
            for stale_rhash in (evicted or []):
                self._delete_stale_request(stale_rhash)
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

    # --- kind-0 metadata advertising (clink_offer) ------------------------

    async def _fetch_metadata_content(self) -> Optional[str]:
        """The newest kind-0 content string published for our identity.

        Several relays may each answer with a stored event; the latest
        ``created_at`` wins. ``None`` when no relay has any metadata for us.
        """
        newest: Optional[nEvent] = None
        async for ev in self.manager.get_events(
                {"kinds": [0], "authors": [self.pubkey_hex], "limit": 1},
                only_stored=True):
            if newest is None or ev.created_at > newest.created_at:
                newest = ev
        return newest.content if newest is not None else None

    async def _publish_metadata_content(self, content: str) -> None:
        """Publish a signed kind-0 event to every connected relay."""
        await aionostr._add_event(
            self.manager, kind=0, content=content, tags=[],
            private_key=self.private_key.hex())

    async def sync_metadata(self) -> bool:
        """Reconcile the kind-0 ``clink_offer`` field with the default offer.

        Fetches our current metadata, merges the default offer's noffer in
        (preserving every other profile field), and republishes only when the
        content actually changed — so a connected relay is never spammed with
        identical events. Returns True when a publish happened. Safe to call
        any time: no-ops while the manager is down, metadata advertising is
        disabled, or there is nothing to advertise yet.
        """
        if self.manager is None or not self.advertise_metadata:
            return False
        default = default_offer(self.offers.list())
        noffer = self.make_noffer(default.offer_id) if default else None
        try:
            existing = await self._fetch_metadata_content()
        except Exception:
            self.logger.debug("could not fetch clink metadata", exc_info=True)
            return False
        target = merge_clink_offer(existing, noffer)
        if target == existing:
            return False
        if not noffer and not (existing or "").strip():
            return False
        try:
            await self._publish_metadata_content(target)
        except Exception:
            self.logger.exception("could not publish clink metadata")
            return False
        self.logger.info(
            "advertised clink offer in kind-0 metadata"
            + (f" ({default.offer_id})" if default else " (removed)"))
        return True

    def _schedule_metadata_sync(self) -> None:
        """Fire ``sync_metadata`` onto the asyncio loop, if one is running.

        Called from the plugin API (cmdline/Qt thread) after an offer mutation
        changes which noffer is the default, so the advertisement follows the
        offer without blocking the caller on a relay round trip.
        """
        try:
            loop = get_asyncio_loop()
        except Exception:
            return
        if loop is None or not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.sync_metadata(), loop)
        except Exception:
            self.logger.debug("could not schedule clink metadata sync", exc_info=True)

    def _delete_stale_request(self, rhash: str) -> None:
        """Garbage-collect the wallet request behind a dropped registry entry.

        Every CLINK invoice creates a persisted wallet request; without this,
        an attacker spamming requests would grow the wallet DB without bound.
        Deletes only a still-unpaid (or expired) request — a paid one is the
        merchant's record, and an in-flight payment must be able to settle.
        """
        try:
            request = self.wallet.get_request(rhash)
            if request is None:
                return
            if self.wallet.get_invoice_status(request) in (PR_UNPAID, PR_EXPIRED):
                self.wallet.delete_request(rhash)
                self.logger.debug(f"GC'd expired clink request {rhash[:10]}…")
        except Exception:
            self.logger.exception(f"could not GC clink request {rhash[:10]}…")

    # --- payment receipts ------------------------------------------------

    def _preimage_for(self, rhash: str) -> Optional[str]:
        """The settlement preimage of a paid invoice, for its payment receipt.

        Invoices we issued carry their preimage in the wallet's preimage store
        from creation, so ``get_preimage_hex`` answers immediately on ``PR_PAID``.
        Returns ``None`` (an "internal settlement" receipt, per the CLINK spec)
        if the wallet or its lnworker is ever not ready.
        """
        try:
            lnworker = getattr(self.wallet, "lnworker", None)
            if lnworker is None:
                return None
            return lnworker.get_preimage_hex(rhash)
        except Exception:
            self.logger.exception(f"could not read preimage for {rhash[:10]}…")
            return None

    async def _deliver_receipt(self, target: ReceiptTarget) -> bool:
        """Publish the payment receipt for a settled invoice.

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
                    target.payer_pubkey, target.request_event_id,
                    protocol.receipt_payload(target.preimage)),
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
        The relay liveness check piggybacks on the same tick (after the
        receipts, so its retry delay never postpones a receipt).
        """
        while True:
            try:
                # Sweeping returns the expired-unpaid invoices it dropped; their
                # persisted wallet requests are garbage-collected alongside.
                for stale_rhash in self.receipts.sweep():
                    self._delete_stale_request(stale_rhash)
                for target in self.receipts.due_targets():
                    await self._deliver_receipt(target)
            except Exception:
                self.logger.exception("error redelivering receipts")
            try:
                await self.check_relay_liveness()
            except Exception:
                self.logger.exception("error checking relay liveness")
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
        # (mark_due, capturing the settlement preimage now) and fire a
        # best-effort delivery on the asyncio loop. The entry stays owed until
        # the relay accepts it, so a drop here is retried.
        target = self.receipts.mark_due(request.rhash, preimage=self._preimage_for(request.rhash))
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
                self._record("devfee", amount_sat, "dev-fee payment failed")
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
                           relay: str = "", price: Optional[int] = None,
                           expires_in: Optional[int] = None) -> Dict[str, Any]:
        """Create an offer; ``relay`` (``wss://…``) pins a custom relay instead
        of the automatic pick. ``price`` (positive sats) turns it into a fixed-
        price offer whose noffer advertises TLV 3/4 and whose invoices are always
        minted at exactly that amount; ``None`` keeps it spontaneous. Either way
        the relay is payability-probed first and a failing probe *blocks*
        creation, and the chosen relay is pinned onto the offer so the noffer
        handed to payers never changes across restarts. ``expires_in`` (seconds)
        sets an absolute ``expires_at`` on the offer: past it, payers get a
        code-3 "expired" response instead of an invoice."""
        assert self.server is not None, "wallet not loaded yet"
        custom_relay = (relay or "").strip()
        if custom_relay:
            return await self._create_offer_custom_relay(
                custom_relay, label=label, allow_payer_memo=allow_payer_memo,
                price=price, expires_in=expires_in)
        # Probe (cached 24h) before building the noffer so its single embedded
        # relay is one a payer can actually reach — see clink.relay_probe.
        selection = await self.server.pick_payable_relay()
        if not selection.ok or not selection.relay:
            tried = ", ".join(r.relay for r in selection.results) or "(none configured)"
            raise UserFacingException(
                "No Nostr relay accepted a test payment request, so the offer "
                f"would not be payable and was not created. Tried: {tried}. "
                "Set a working relay in the CLINK settings and try again.")
        # The listener must sit on the pinned relay too; usually it already
        # does (pick_payable_relay restarts it on a changed pick), but a relay
        # re-picked from the 24h cache may not be covered yet.
        needs_listener = selection.relay not in self.server.listen_relay_urls()
        offer = self.server.offers.create(
            label=label, allow_payer_memo=allow_payer_memo,
            price_type=_offer_price_type(price), price=_offer_price(price),
            relay=selection.relay, relay_custom=False,
            expires_at=_offer_expires_at(expires_in))
        if needs_listener:
            self.server.restart_event_handler()
        self.server._schedule_metadata_sync()
        return {
            "offer_id": offer.offer_id, "label": offer.label,
            "allow_payer_memo": offer.allow_payer_memo,
            "price_type": int(offer.price_type), "price": offer.price,
            "noffer": self.server.make_noffer(offer.offer_id),
            "relay": offer.relay,
            "relay_payable": True,
            "expires_at": offer.expires_at,
        }

    async def _create_offer_custom_relay(self, relay: str, *, label: str,
                                         allow_payer_memo: bool,
                                         price: Optional[int] = None,
                                         expires_in: Optional[int] = None) -> Dict[str, Any]:
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
            label=label, allow_payer_memo=allow_payer_memo,
            price_type=_offer_price_type(price), price=_offer_price(price),
            relay=relay, relay_custom=True,
            expires_at=_offer_expires_at(expires_in))
        if needs_listener:
            self.server.restart_event_handler()
        self.server._schedule_metadata_sync()
        return {
            "offer_id": offer.offer_id, "label": offer.label,
            "allow_payer_memo": offer.allow_payer_memo,
            "price_type": int(offer.price_type), "price": offer.price,
            "noffer": self.server.make_noffer(offer.offer_id),
            "relay": relay,
            "relay_payable": True,
            "expires_at": offer.expires_at,
        }

    def list_offers(self) -> Dict[str, Any]:
        # Read-only status getters tolerate a missing server (wallet not yet
        # loaded, or torn down on shutdown while the Qt poller is still firing).
        if self.server is None:
            return {}
        return {o.offer_id: {"label": o.label, "active": o.active,
                             "allow_payer_memo": o.allow_payer_memo,
                             "price_type": int(o.price_type), "price": o.price,
                             "noffer": self.server.make_noffer(o.offer_id),
                             "relay": self.server.offer_relay(o),
                             "relay_custom": o.relay_custom,
                             # False only for a pre-pinning legacy offer whose
                             # relay is still re-derived from the config order.
                             "relay_pinned": bool(o.relay.strip()),
                             "expires_at": o.expires_at,
                             "replaced_by": o.replaced_by}
                for o in self.server.offers.list()}

    def candidate_relay_urls(self) -> List[str]:
        """Relays offered in the Qt "New offer" relay dropdown (config order)."""
        if self.server is None:
            return []
        return self.server.candidate_relays()

    async def advertise(self) -> Dict[str, Any]:
        """Reconcile the kind-0 ``clink_offer`` advertisement right now.

        Returns the outcome plus the advertised (or to-be-advertised) noffer so
        callers can tell a fresh publish from an already-correct one.
        """
        assert self.server is not None, "wallet not loaded yet"
        published = await self.server.sync_metadata()
        status = self.metadata_status()
        status["published"] = bool(published)
        return status

    def metadata_status(self) -> Dict[str, Any]:
        """The current default-offer advertisement (no network I/O).

        ``offer_id``/``noffer`` are what kind-0 metadata should advertise for
        the live offer set; ``enabled`` reflects the advertising config.
        """
        if self.server is None:
            return {"enabled": bool(self.config.CLINK_ADVERTISE_METADATA),
                    "offer_id": None, "noffer": None}
        default = default_offer(self.server.offers.list())
        return {
            "enabled": self.server.advertise_metadata,
            "offer_id": default.offer_id if default else None,
            "noffer": self.server.make_noffer(default.offer_id) if default else None,
        }

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
        if removed:
            self.server._schedule_metadata_sync()
        return removed

    def replace_offer(self, offer_id: str, replacement_id: str) -> bool:
        """Move ``offer_id`` onto ``replacement_id``: the outgoing offer stops
        answering and requests for it get a code-3 response carrying a
        ``latest`` noffer pointing at the replacement (see the spec), so a
        payer holding the stale noffer updates and retries."""
        assert self.server is not None, "wallet not loaded yet"
        if not self.server.offers.replace_with(offer_id, replacement_id):
            return False
        ok = self.server.offers.set_active(offer_id, False)
        self.server._schedule_metadata_sync()
        return ok

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

    def relay_liveness(self) -> Dict[str, Dict[str, Any]]:
        """Latest per-relay liveness results (session-only; may be empty)."""
        if self.server is None:
            return {}
        return {relay: res.to_dict()
                for relay, res in self.server.liveness.results.items()}

    async def check_relays(self, retry_delay: Optional[float] = None) -> Dict[str, Any]:
        """Run the relay liveness check now (the hourly one runs on its own).

        ``retry_delay`` overrides the pause before a failed relay's confirming
        re-probe (mainly for tests); omit for the default.
        """
        assert self.server is not None, "wallet not loaded yet"
        results = await self.server.check_relay_liveness(retry_delay=retry_delay)
        return {relay: res.to_dict() for relay, res in results.items()}

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
