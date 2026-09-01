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
import secrets
import ssl
import time
from collections import OrderedDict, deque
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set

import electrum_aionostr as aionostr
from electrum_aionostr.event import Event as nEvent
from electrum_aionostr.key import PrivateKey
from electrum_aionostr.util import normalize_url

from electrum.logging import Logger
from electrum.plugin import BasePlugin, hook
from electrum.invoices import PR_EXPIRED, PR_PAID, PR_UNPAID, Invoice, Request
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
from .nostr_transport import HeartbeatManager
from .publish import PublishRejected, add_event_checked
from .liquidity import LiquidityReserver, receivable_capacity_sat
from .liveness import LivenessResult, RelayLivenessMonitor
from .noffer import Noffer, OfferPriceType, noffer_encode
from .offers import Offer, OfferStore, advertised_relay, advertised_relays, listen_relays
from .receipts import (RESEND_OFFSETS_SEC, RETRY_INTERVAL_SEC,
                       ReceiptRegistry, ReceiptTarget)
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
# The relay watchdog (interval: CLINK_WATCHDOG_INTERVAL_SEC) defends the
# listener against three silent failure modes:
#   * Manager.connect() drops relays it could not reach and never retries them
#     on its own -> ensure_listener_relays re-attaches missing relays.
#   * A connection aiohttp already knows is dead (heartbeat missed a pong, the
#     receive loop crashed) stays in manager.relays looking healthy
#     -> prune_dead_relays drops it so the re-attach picks it up.
#   * A half-open TCP connection (NAT/proxy idle cull) looks alive to every
#     state check while delivering nothing -> ping_listener round-trips a
#     self-addressed event through each relay (every
#     CLINK_LISTENER_PING_INTERVAL_SEC) and reconnects the ones that go quiet.
# How long ping_listener waits for its self-addressed events to echo back.
LISTENER_PING_TIMEOUT_SEC = 30
# Budget for publishing one ping to one relay (bounds Relay.send's internal
# reconnect loop, which can otherwise sleep for many minutes).
LISTENER_PING_SEND_TIMEOUT_SEC = 10


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
        # In-flight listener self-pings: ping event id -> set when it echoes
        # back through the live subscription (see ping_listener).
        self._pending_pings: Dict[str, asyncio.Event] = {}
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
        # Payment hashes with an in-session re-broadcast tail scheduled, so the
        # immediate-delivery path and the redelivery tick can't stack duplicate
        # tails for the same receipt. Session-only: after a restart the
        # registry's due_targets resumes any interrupted schedule.
        self._receipt_tails: "set[str]" = set()
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
    def ws_heartbeat_sec(self) -> float:
        """Websocket ping interval for listener relay connections (0 disables)."""
        return max(0.0, float(self.config.CLINK_WS_HEARTBEAT_SEC))  # type: ignore[attr-defined]

    @property
    def watchdog_interval_sec(self) -> int:
        return max(1, int(self.config.CLINK_WATCHDOG_INTERVAL_SEC))  # type: ignore[attr-defined]

    @property
    def listener_ping_interval_sec(self) -> int:
        """Cadence of the end-to-end listener self-ping (0 disables)."""
        return max(0, int(self.config.CLINK_LISTENER_PING_INTERVAL_SEC))  # type: ignore[attr-defined]

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
        return noffer_encode(Noffer(
            pubkey=self.pubkey_hex,
            relay=self.offer_relay(self.offers.get(offer_id)),
            offer=offer_id,
            price_type=OfferPriceType.SPONTANEOUS,
        ))

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
        # HeartbeatManager (not the plain aionostr.Manager): its websockets
        # ping the relay and fail fast on a missed pong, so a silently dead
        # connection surfaces instead of leaving the listener deaf forever.
        return HeartbeatManager(
            relays=relays,
            private_key=self.private_key.hex(),
            log=nostr_logger,
            ssl_context=self.ssl_context,
            proxy=factory() if factory else None,
            heartbeat_sec=self.ws_heartbeat_sec,
        )

    def listener_prerequisites_met(self) -> bool:
        """Whether the listener has everything it needs to come up.

        Gates on :meth:`listen_relay_urls` — what the listener will actually
        sit on — not on the raw config relay list: an offer pinned to a custom
        relay must be served even when ``NOSTR_RELAYS`` is empty (the old
        ``relay_url`` gate silently never started the listener then).
        """
        return bool(self.listen_relay_urls()
                    and self.wallet.network
                    and self.wallet.network.is_connected()
                    and self.wallet.lnworker)

    @staticmethod
    def _run_task_was_cancelled() -> bool:
        """Whether *our own* task got ``.cancel()``'d (plugin stop, app
        shutdown teardown) — as opposed to a ``CancelledError`` that bubbled
        up out of a child of the inner taskgroup.

        The distinction is what keeps Electrum's process exit working: at
        shutdown the event-loop teardown cancels every task exactly once and
        then waits for all of them (``electrum.util.run_event_loop``), so a
        task that swallows that one cancel — as the restart-on-cancel loop
        below used to — leaves the non-daemon EventLoop thread, and thus the
        whole process, hanging forever.

        On Python < 3.11 (no ``Task.cancelling``) every cancel counts as
        external: hang-proof, at the cost of not auto-restarting after a
        stray child cancellation.
        """
        task = asyncio.current_task()
        cancelling = getattr(task, "cancelling", None)
        return True if cancelling is None else cancelling() > 0

    @log_exceptions
    async def run(self) -> None:
        while True:
            while not self.listener_prerequisites_met():
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
                    await tg.spawn(self._relay_watchdog())
                    await tg.spawn(self._devfee_startup_check())
                    await tg.spawn(self._redeliver_receipts())
                # A requested restart (restart_event_handler) cancels the
                # *children*, which OldTaskGroup.join skips over — the group
                # exits normally and the while-loop rebuilds the listener.
            except asyncio.CancelledError:
                if self.do_stop or self._run_task_was_cancelled():
                    raise
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

    async def ensure_listener_relays(self) -> None:
        """Re-attach the listener to any relay it should sit on but lost.

        ``Manager.connect()`` drops relays it could not reach from
        ``manager.relays`` and never retries them, while ``_manager_relays``
        records the *requested* set — so :meth:`refresh_manager` sees no drift
        and a pinned relay that was down for a moment at (re)connect time
        would otherwise stay missing (its offers silently unpayable) for the
        rest of the session. Called periodically by :meth:`_relay_watchdog`.
        """
        manager = self.manager
        if manager is None or not manager.connected:
            return
        desired = {normalize_url(u) for u in self.listen_relay_urls()}
        connected = {relay.url for relay in manager.relays}
        if connected >= desired:
            return
        missing = sorted(desired - connected)
        self.logger.info(
            f"listener is missing relay(s) {', '.join(missing)}; reconnecting")
        # update_relays connects the missing relays and re-issues the open
        # subscriptions on them; ones still down are dropped again and simply
        # retried on the next watchdog tick.
        await manager.update_relays(sorted(desired))

    @staticmethod
    def _relay_connection_dead(relay: Any) -> bool:
        """Whether ``relay``'s connection is observably broken.

        Catches states the membership check can't: a websocket aiohttp already
        closed (e.g. the heartbeat missed a pong, or the peer reset us while
        the library's own reconnect is stuck in its multi-minute backoff) or a
        crashed/finished receive loop. A half-open connection still looks
        healthy here — that is what :meth:`ping_listener` exists for.
        """
        if not relay.connected:
            return True
        if relay.ws is None or relay.ws.closed:
            return True
        task = relay.receive_task
        return task is not None and task.done()

    async def prune_dead_relays(self) -> None:
        """Drop relays with observably dead connections from the manager, so
        the watchdog's membership pass (:meth:`ensure_listener_relays`)
        re-attaches them with fresh connections and re-issued subscriptions."""
        manager = self.manager
        if manager is None or not manager.connected:
            return
        dead = [r for r in manager.relays if self._relay_connection_dead(r)]
        if not dead:
            return
        self.logger.info(
            "dropping dead relay connection(s): "
            + ", ".join(r.url for r in dead))
        await self._drop_relays(dead)

    async def _drop_relays(self, relays: List[Any]) -> None:
        """Close ``relays`` and remove them from the live manager.

        The next :meth:`ensure_listener_relays` sees them missing and brings
        up fresh connections (with re-issued subscriptions) in their place — a
        targeted teardown of connections that are already broken, leaving the
        healthy relays' service untouched.
        """
        manager = self.manager
        if manager is None:
            return
        for relay in relays:
            try:
                # Bounded: ws.close on a half-open connection waits ~10s for a
                # close frame that will never come.
                await asyncio.wait_for(relay.close(), timeout=15)
            except Exception:
                self.logger.debug(f"error closing relay {relay.url}", exc_info=True)
        manager.relays = [r for r in manager.relays if r not in relays]

    def _make_listener_ping(self) -> nEvent:
        """A self-addressed kind-21001 event used to prove a relay round-trip.

        Kind 21001 is ephemeral (relays broadcast but never store it) and the
        ``p`` tag is our own pubkey, so it reaches exactly our own listener
        subscription and nobody else's. The nonce makes each ping's event id
        unique even within one second, letting one ping identify one relay.
        """
        event = nEvent(
            pubkey=self.pubkey_hex,
            kind=CLINK_EVENT_KIND,
            tags=[["p", self.pubkey_hex]],
            content=json.dumps({"clink_listener_ping": secrets.token_hex(8)}),
        )
        return event.sign(self.private_key.hex())

    async def ping_listener(self, *, timeout: float = LISTENER_PING_TIMEOUT_SEC) -> bool:
        """Verify the listener actually hears each relay it sits on.

        Publishes one self-addressed ping per connected relay and waits for
        every ping to come back through the live subscription. A relay whose
        ping does not echo within ``timeout`` has a dead receive path — the
        deaf-listener state a half-open TCP connection causes, which no
        connection-state check can see — so it is dropped and immediately
        re-attached (fresh connection + re-issued subscription). Returns
        whether every relay passed.
        """
        manager = self.manager
        if manager is None or not manager.connected or not manager.relays:
            return True  # nothing to verify; (re)connecting is run()'s job
        waiters: Dict[str, asyncio.Event] = {}  # ping event id -> arrival flag
        pinged: Dict[str, Any] = {}             # ping event id -> relay
        for relay in list(manager.relays):
            ping = self._make_listener_ping()
            waiter = asyncio.Event()
            self._pending_pings[ping.id] = waiter
            waiters[ping.id] = waiter
            pinged[ping.id] = relay
            try:
                await asyncio.wait_for(
                    relay.add_event(ping.to_json_object()),
                    timeout=LISTENER_PING_SEND_TIMEOUT_SEC)
            except Exception as e:
                # A failed send is handled the same as a missing echo below.
                self.logger.info(f"listener ping send failed on {relay.url}: {e!r}")
        try:
            await asyncio.wait_for(
                asyncio.gather(*(w.wait() for w in waiters.values())),
                timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            for ping_id in waiters:
                self._pending_pings.pop(ping_id, None)
        failed = [pinged[pid] for pid, w in waiters.items() if not w.is_set()]
        if not failed:
            self.logger.debug("listener self-ping ok on all relays")
            return True
        urls = ", ".join(r.url for r in failed)
        self.logger.warning(
            f"listener self-ping got no echo from {urls}; "
            f"dropping and re-attaching the connection(s)")
        self._record("listener", None, f"relay went silent, reconnecting: {urls[:64]}")
        await self._drop_relays(failed)
        await self.ensure_listener_relays()
        return False

    async def _relay_watchdog(self) -> None:
        next_ping = time.monotonic() + self.listener_ping_interval_sec
        while True:
            await asyncio.sleep(self.watchdog_interval_sec)
            try:
                await self.prune_dead_relays()
                await self.ensure_listener_relays()
            except Exception:
                self.logger.exception("error reconnecting listener relays")
            ping_interval = self.listener_ping_interval_sec
            if ping_interval and time.monotonic() >= next_ping:
                next_ping = time.monotonic() + ping_interval
                try:
                    await self.ping_listener()
                except Exception:
                    self.logger.exception("error self-pinging the listener")

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
        # A live (only_stored=False) subscription must never end on its own —
        # if it does (e.g. a poisoned end-of-stream sentinel from the relay
        # layer), returning here would leave run()'s taskgroup humming along
        # with no listener: offers silently unpayable for the rest of the
        # session. Raise instead so run() tears the manager down and rebuilds.
        raise RuntimeError("listener event stream ended unexpectedly")

    def _already_seen(self, event_id: str) -> bool:
        if event_id in self._seen_events:
            return True
        self._seen_events[event_id] = None
        while len(self._seen_events) > SEEN_EVENTS_MAX:
            self._seen_events.popitem(last=False)
        return False

    async def _dispatch(self, event: nEvent) -> None:
        # Listener self-pings (watchdog liveness probes addressed to ourselves)
        # are consumed here — their arrival IS the signal (see ping_listener).
        ping_waiter = self._pending_pings.pop(event.id, None)
        if ping_waiter is not None:
            ping_waiter.set()
            return
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
        resolution = protocol.resolve_request(req, offer, self.reserver.available_sat())
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
        await self._issue_invoice(
            event, offer, resolution.amount_sat, description, selftest=selftest)

    def _record(self, offer_id: str, amount: Optional[int], result: str) -> None:
        # offer_id can originate from a hostile payer; keep the activity feed
        # (and the Qt tab rendering it) bounded per entry.
        self.recent_activity.append({
            "time": int(time.time()), "offer": str(offer_id)[:32],
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
        await tg.spawn(self._publish_response(request_event, payload))

    async def _publish_response(self, request_event: nEvent, payload: Dict[str, Any]) -> None:
        """Encrypt and publish a response, surfacing relay rejections.

        A rejection (``OK false``) is logged with the relay's reason instead of
        passing silently; other failures (timeout, connection trouble) keep
        their old behaviour — the exception restarts the event handler, which
        reconnects the relays.
        """
        try:
            await add_event_checked(self.manager, **self._encrypt_event_args(
                request_event.pubkey, request_event.id, payload))
        except PublishRejected as e:
            self.logger.warning(
                f"relay rejected our response to {request_event.id[:10]}…: {e}")
            self._record("response", None, f"relay rejected response: {e}")

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

    async def _deliver_receipt(self, target: ReceiptTarget,
                               fail_streak: int = 0) -> bool:
        """Publish the ``{"res":"ok"}`` receipt for a settled invoice.

        Best-effort and idempotent: stamps the attempt first, awaits the relay
        publish, and on success records the send — the receipt is an ephemeral
        event the payer may have missed, so the entry stays owed and a
        re-broadcast tail is scheduled until the
        :data:`~clink.receipts.RESEND_OFFSETS_SEC` schedule completes (see the
        README's "Receipt re-broadcasts" section). A *failed* publish inside
        the fast-retry window schedules a short-backoff retry tail
        (``fail_streak`` counts the consecutive failures that produced the
        current backoff; a success resets it); outside the window the hourly
        redelivery loop takes over. Never raises.
        """
        self.receipts.record_attempt(target.rhash)
        if self.manager is None:
            return False
        try:
            # Checked publish: an ``OK false`` counts as a failed delivery (and
            # names the relay's reason in the retry log) instead of silently
            # marking an undelivered receipt as sent.
            await asyncio.wait_for(add_event_checked(
                self.manager,
                **self._encrypt_event_args(
                    target.payer_pubkey, target.request_event_id, protocol.receipt_payload()),
            ), timeout=30)
        except Exception as e:
            retry_delay = self.receipts.fail_retry_delay(target.rhash, fail_streak + 1)
            if retry_delay is not None:
                self.logger.warning(
                    f"receipt delivery failed for {target.rhash[:10]}… "
                    f"(attempt {target.attempts + 1}); retrying in "
                    f"{retry_delay:.0f}s: {e!r}")
                await self._schedule_receipt_rebroadcast(
                    target, retry_delay, fail_streak=fail_streak + 1)
            else:
                self.logger.warning(
                    f"receipt delivery failed for {target.rhash[:10]}… "
                    f"(attempt {target.attempts + 1}); will retry hourly: {e!r}")
            return False
        next_delay = self.receipts.record_send(target.rhash)
        if target.sends == 0:
            self.logger.info(
                f"receipt delivered to {target.payer_pubkey[:10]}… for "
                f"{target.rhash[:10]}…; re-broadcasting {len(RESEND_OFFSETS_SEC)}x "
                f"in case the payer missed it")
            self._record("receipt", None, "receipt sent ✓")
        else:
            self.logger.debug(
                f"receipt re-broadcast {target.sends + 1}/{len(RESEND_OFFSETS_SEC) + 1} "
                f"for {target.rhash[:10]}…")
        if next_delay is not None:
            await self._schedule_receipt_rebroadcast(
                replace(target, sends=target.sends + 1), next_delay)
        return True

    async def _schedule_receipt_rebroadcast(self, target: ReceiptTarget,
                                            delay: float, *,
                                            fail_streak: int = 0) -> None:
        """Spawn the re-broadcast/retry tail for ``target``, one per rhash."""
        tg = self.taskgroup
        if tg is None or target.rhash in self._receipt_tails:
            return
        self._receipt_tails.add(target.rhash)
        await tg.spawn(self._rebroadcast_receipt_later(target, delay, fail_streak))

    async def _rebroadcast_receipt_later(self, target: ReceiptTarget,
                                         delay: float,
                                         fail_streak: int = 0) -> None:
        """Sleep out one schedule/backoff step, then re-send if still owed.

        Runs inside the relay taskgroup: a disconnect cancels the sleep, and the
        registry's due_targets resumes the schedule on reconnect instead.
        """
        try:
            await asyncio.sleep(delay)
        finally:
            # Clear the guard before delivering — a successful delivery
            # schedules the *next* tail, which must not be blocked by this one.
            self._receipt_tails.discard(target.rhash)
        if not self.receipts.is_owed(target.rhash):
            return  # completed (or abandoned) by another path meanwhile
        await self._deliver_receipt(target, fail_streak)

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
        self.taskgroup: OldTaskGroup = OldTaskGroup()

    @staticmethod
    def _wallet_file(wallet: "Abstract_Wallet") -> Optional[str]:
        """The wallet's on-disk path, used as its stable identity across
        close/reopen cycles (each reload builds a fresh wallet object)."""
        try:
            return wallet.storage.path if wallet.storage else None
        except Exception:
            return None

    def start_plugin(self, wallet: "Abstract_Wallet") -> None:
        if not wallet.has_lightning():
            self.logger.info("wallet has no lightning; CLINK offers need it to issue invoices")
            return
        server = self.server
        if server is not None:
            if server.wallet is wallet:
                return  # already driving this very wallet
            same_file = (self._wallet_file(server.wallet) is not None
                         and self._wallet_file(server.wallet) == self._wallet_file(wallet))
            if not same_file:
                return  # only drive a single wallet
            # Same wallet file, new object: the wallet was closed and reopened
            # without the Qt close hook firing (daemon close_wallet/load_wallet
            # never fires it). The old server drives a dead wallet — replace it,
            # or offers would silently stop being answered until a full restart.
            self.logger.info("driven wallet was reloaded; restarting the CLINK server")
            self._stop_server()
        # A fresh taskgroup per server generation: the old one was cancelled
        # when its server stopped and must not adopt the new run loop.
        self.taskgroup = OldTaskGroup()
        self.server = ClinkServer(self.config, wallet, self)
        self._run_async(self.taskgroup.spawn(self.server.run()))
        self.logger.info("CLINK plugin started")

    def _run_async(self, coro: Any) -> None:
        """Schedule ``coro`` on Electrum's asyncio loop (patchable in tests)."""
        asyncio.run_coroutine_threadsafe(coro, get_asyncio_loop())

    def _stop_server(self) -> None:
        """Stop and detach the current server (idempotent, restartable).

        Unlike the pre-0.0.7 close path this leaves the plugin restartable: a
        later ``start_plugin`` builds a fresh server and taskgroup instead of
        being blocked by a sticky ``initialized`` flag.
        """
        server, taskgroup = self.server, self.taskgroup
        self.server = None
        if server is None:
            return
        server.do_stop = True
        server.unregister_callbacks()

        async def close() -> None:
            # The taskgroup must be cancelled even when the relay teardown
            # errors: a still-running server.run() task past this point can
            # only be reaped by the process-exit teardown.
            try:
                if server.manager:
                    await server.manager.close()
            finally:
                await taskgroup.cancel_remaining()
        self._run_async(close())

    @hook
    def close_wallet(self, wallet: Optional["Abstract_Wallet"] = None, *args, **kwargs):
        # Only a close of the wallet we drive stops the server: closing some
        # *other* wallet window used to kill the listener for the rest of the
        # session (offers kept looking fine but no request was ever answered).
        server = self.server
        if server is None:
            return
        if wallet is not None and server.wallet is not wallet:
            # A different wallet *object* can still be the same wallet file,
            # reopened behind our back (daemon close/load cycles never fire
            # this hook): the server then drives a dead wallet, and skipping
            # the stop here would leave it running — unstoppably — into the
            # process-exit teardown. Only a genuinely different wallet file
            # leaves the server alone.
            ours = self._wallet_file(server.wallet)
            theirs = self._wallet_file(wallet)
            if ours is None or theirs is None or ours != theirs:
                return
        self._stop_server()

    def on_close(self) -> None:
        """Called by BasePlugin.close() when the plugin is disabled.

        By then the hooks are already unregistered, so close_wallet can never
        fire again — without this stop the listener would keep answering
        offers while "disabled", and its run() task would hang process exit.
        """
        self._stop_server()

    # --- API used by cmdline + Qt ----------------------------------------

    async def create_offer(self, label: str = "", allow_payer_memo: bool = True,
                           relay: str = "") -> Dict[str, Any]:
        """Create an offer; ``relay`` (``wss://…``) pins a custom relay instead
        of the automatic pick. Either way the relay is payability-probed first
        and a failing probe *blocks* creation, and the chosen relay is pinned
        onto the offer so the noffer handed to payers never changes across
        restarts."""
        assert self.server is not None, "wallet not loaded yet"
        custom_relay = (relay or "").strip()
        if custom_relay:
            return await self._create_offer_custom_relay(
                custom_relay, label=label, allow_payer_memo=allow_payer_memo)
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
            relay=selection.relay, relay_custom=False)
        if needs_listener:
            self.server.restart_event_handler()
        return {
            "offer_id": offer.offer_id, "label": offer.label,
            "allow_payer_memo": offer.allow_payer_memo,
            "noffer": self.server.make_noffer(offer.offer_id),
            "relay": offer.relay,
            "relay_payable": True,
        }

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
            label=label, allow_payer_memo=allow_payer_memo, relay=relay,
            relay_custom=True)
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
                             "relay_custom": o.relay_custom,
                             # False only for a pre-pinning legacy offer whose
                             # relay is still re-derived from the config order.
                             "relay_pinned": bool(o.relay.strip())}
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
