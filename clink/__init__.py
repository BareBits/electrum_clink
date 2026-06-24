"""CLINK plugin for Electrum — generate noffers and answer requests with invoices.

Registers the plugin's config vars and command-line API. The runtime lives in
:mod:`clink.clink_plugin`; the protocol/crypto building blocks are in the sibling
modules and are independently unit-tested.
"""

from typing import TYPE_CHECKING

from electrum.commands import plugin_command
from electrum.simple_config import SimpleConfig, ConfigVar

if TYPE_CHECKING:
    from electrum.commands import Commands
    from .clink_plugin import ClinkPlugin

plugin_name = "clink"

# Relay the plugin subscribes to and advertises in every noffer. Empty -> fall
# back to Electrum's global NOSTR_RELAYS (first entry). The rig injects its local
# relay here for development.
SimpleConfig.CLINK_RELAY = ConfigVar(
    key="plugins.clink.relay",
    default="",
    type_=str,
    plugin=plugin_name,
)

# How long an issued invoice stays valid AND its inbound liquidity stays locked.
SimpleConfig.CLINK_INVOICE_EXPIRY = ConfigVar(
    key="plugins.clink.invoice_expiry_sec",
    default=120,
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
async def add_offer(self: "Commands", label: str = "", plugin: "ClinkPlugin" = None) -> dict:
    """
    Create a new spontaneous offer and return its noffer string.

    arg:str:label:optional human label for the offer
    """
    return plugin.create_offer(label)


@plugin_command("", plugin_name)
async def list_offers(self: "Commands", plugin: "ClinkPlugin" = None) -> dict:
    """
    List all offers with their noffer strings.
    """
    return plugin.list_offers()


@plugin_command("", plugin_name)
async def remove_offer(self: "Commands", offer_id: str, plugin: "ClinkPlugin" = None) -> str:
    """
    Remove an offer by its id.
    arg:str:offer_id:offer id, see list_offers
    """
    ok = plugin.remove_offer(offer_id)
    return f"removed {offer_id}" if ok else f"no such offer: {offer_id}"


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
