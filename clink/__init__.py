"""CLINK plugin for Electrum — generate noffers and answer requests with invoices.

Registers the plugin's config vars and command-line API. The runtime lives in
:mod:`clink.clink_plugin`; the protocol/crypto building blocks are in the sibling
modules and are independently unit-tested.
"""

# --- external-plugin loader shim (Electrum 4.7.x) --------------------------
# When Electrum installs us from a zip, its loader registers this package in
# ``sys.modules`` under ``electrum_external_plugins.clink`` but (a) never creates
# the parent ``electrum_external_plugins`` namespace package and (b) leaves this
# module's ``__name__``/``__package__`` as the bare in-zip name ``clink``. The
# second bug is fatal: the first cross-module relative import in a submodule
# (e.g. ``from . import nip44, protocol`` in clink_plugin) makes CPython build
# the absolute child name from this package's ``__name__``, so it looks for a
# top-level ``clink.nip44`` and raises ``ModuleNotFoundError: No module named
# 'clink'``. We repair both here, before any submodule import runs, so the
# distributed zip loads on a stock Electrum. The dev rig loads us as an internal
# plugin instead, so this is inert there (the guard below only fires for the
# zip-install identity).
def _repair_external_plugin_identity() -> None:
    import sys
    import types

    ns_name = "electrum_external_plugins"
    full_name = f"{ns_name}.clink"
    # Only act when we are *this* module registered under the external-zip key;
    # never hijack an internal load (electrum.plugins.clink) or a plain import.
    me = sys.modules.get(full_name)
    if me is None or me.__dict__ is not globals():
        return
    if ns_name not in sys.modules:
        ns = types.ModuleType(ns_name)
        ns.__path__ = []  # namespace package with no on-disk location
        sys.modules[ns_name] = ns
    if __name__ != full_name:
        globals()["__name__"] = full_name
        globals()["__package__"] = full_name
        if __spec__ is not None:
            __spec__.name = full_name


_repair_external_plugin_identity()
del _repair_external_plugin_identity
# ---------------------------------------------------------------------------

from typing import TYPE_CHECKING

from electrum.commands import plugin_command
from electrum.simple_config import SimpleConfig, ConfigVar

if TYPE_CHECKING:
    from electrum.commands import Commands
    from .clink_plugin import ClinkPlugin

plugin_name = "clink"

# Relay the plugin prefers when picking (and pinning) the relay for a new
# noffer, and subscribes to. Empty -> fall back to Electrum's global
# NOSTR_RELAYS (first entry). The rig injects its local relay here for
# development.
SimpleConfig.CLINK_RELAY = ConfigVar(
    key="plugins.clink.relay",
    default="",
    type_=str,
    plugin=plugin_name,
)

# How long an issued invoice stays valid AND its inbound liquidity stays locked.
SimpleConfig.CLINK_INVOICE_EXPIRY = ConfigVar(
    key="plugins.clink.invoice_expiry_sec",
    default=300,
    type_=int,
    plugin=plugin_name,
)

# --- Dev fee -------------------------------------------------------------
# An optional, opt-out contribution that funds further plugin development. It
# accrues as a small fraction of inbound payments answered through CLINK offers
# and, once it crosses a threshold, is forwarded to the BareBits dev address.

# Whether the dev fee is collected at all. Opt-out: enabled by default.
SimpleConfig.CLINK_DEVFEE_ENABLED = ConfigVar(
    key="plugins.clink.devfee_enabled",
    default=True,
    type_=bool,
    plugin=plugin_name,
)

# Fee rate as a percentage of each inbound payment (0.001%–5%). Default 0.1%.
SimpleConfig.CLINK_DEVFEE_RATE_PERCENT = ConfigVar(
    key="plugins.clink.devfee_rate_percent",
    default=0.1,
    type_=float,
    plugin=plugin_name,
)

# Lightning address (or LNURL) the accrued fee is forwarded to. A config var so
# the regtest rig can redirect payouts to a local LNURL payee for testing.
SimpleConfig.CLINK_DEVFEE_DEST = ConfigVar(
    key="plugins.clink.devfee_dest",
    default="clink_fees@getbarebits.com",
    type_=str,
    plugin=plugin_name,
)

# Set once the first-run dev-fee notice has been shown, so we only show it once.
SimpleConfig.CLINK_DEVFEE_NOTICE_SHOWN = ConfigVar(
    key="plugins.clink.devfee_notice_shown",
    default=False,
    type_=bool,
    plugin=plugin_name,
)


@plugin_command("", plugin_name)
async def add_offer(self: "Commands", label: str = "", allow_payer_memo: bool = True,
                    relay: str = "", plugin: "ClinkPlugin" = None) -> dict:
    """
    Create a new spontaneous offer and return its noffer string.

    arg:str:label:optional human label for the offer
    arg:bool:allow_payer_memo:whether a payer's requested memo is folded into the invoice (default true)
    arg:str:relay:custom relay URL (wss://myrelay.com:port) the noffer advertises; omit for automatic selection. Either way the relay is probed first (a failing probe blocks creation) and pinned to the offer, so its noffer never changes across restarts.
    """
    return await plugin.create_offer(label, allow_payer_memo, relay)


@plugin_command("", plugin_name)
async def list_offers(self: "Commands", plugin: "ClinkPlugin" = None) -> dict:
    """
    List all offers with their noffer strings.
    """
    return plugin.list_offers()


@plugin_command("", plugin_name)
async def set_offer_label(self: "Commands", offer_id: str, label: str = "",
                          plugin: "ClinkPlugin" = None) -> str:
    """
    Set (or clear) the human label of an existing offer.
    arg:str:offer_id:offer id, see list_offers
    arg:str:label:new label (omit to clear)
    """
    ok = plugin.set_offer_label(offer_id, label)
    return f"updated {offer_id}" if ok else f"no such offer: {offer_id}"


@plugin_command("", plugin_name)
async def set_offer_payer_memo(self: "Commands", offer_id: str, allow: bool,
                               plugin: "ClinkPlugin" = None) -> str:
    """
    Allow or disallow folding a payer-selected memo into invoices for an offer.
    arg:str:offer_id:offer id, see list_offers
    arg:bool:allow:true to honor payer memos, false to always use the label
    """
    ok = plugin.set_offer_allow_payer_memo(offer_id, allow)
    return f"updated {offer_id}" if ok else f"no such offer: {offer_id}"


@plugin_command("", plugin_name)
async def remove_offer(self: "Commands", offer_id: str, plugin: "ClinkPlugin" = None) -> str:
    """
    Remove an offer by its id.
    arg:str:offer_id:offer id, see list_offers
    """
    ok = plugin.remove_offer(offer_id)
    return f"removed {offer_id}" if ok else f"no such offer: {offer_id}"


@plugin_command("", plugin_name)
async def check_noffers(self: "Commands", offer_id: str = "",
                        plugin: "ClinkPlugin" = None) -> dict:
    """
    Verify each offer's noffer is payable end to end: connect to its relay as a
    throwaway payer, send a real (side-effect-free) offer request, and report
    whether a valid invoice came back. Returns {offer_id: result}.
    arg:str:offer_id:check only this offer (omit to check all)
    """
    return await plugin.check_noffers(offer_id or None)


@plugin_command("", plugin_name)
async def check_relays(self: "Commands", retry_delay: int = -1,
                       plugin: "ClinkPlugin" = None) -> dict:
    """
    Probe every relay an existing offer's noffer advertises and report whether
    each can still carry a payment round-trip. The same check runs hourly on its
    own; a failing relay is re-probed once before being reported down. Returns
    {relay_url: result}.
    arg:int:retry_delay:seconds to wait before the confirming re-probe of a failed relay (omit for the default 60)
    """
    return await plugin.check_relays(retry_delay if retry_delay >= 0 else None)


@plugin_command("", plugin_name)
async def clink_status(self: "Commands", plugin: "ClinkPlugin" = None) -> dict:
    """
    Show receivable/reserved inbound liquidity and active reservations.
    """
    return plugin.liquidity_status()


@plugin_command("", plugin_name)
async def devfee_status(self: "Commands", plugin: "ClinkPlugin" = None) -> dict:
    """
    Show dev-fee settings and the current accrued/owed balance.
    """
    return plugin.devfee_status()


@plugin_command("", plugin_name)
async def devfee_pay(self: "Commands", plugin: "ClinkPlugin" = None) -> dict:
    """
    Force an immediate dev-fee payout attempt, ignoring the once-per-day gate
    (the >=1000 sat threshold and 10,000 sat/day cap still apply). For testing.
    """
    return await plugin.devfee_pay_now()
