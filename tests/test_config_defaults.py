"""Unit tests for the plugin's registered config-var defaults.

Importing :mod:`clink` registers the plugin's ``ConfigVar``s on Electrum's
``SimpleConfig``; these tests pin the shipped defaults so an accidental change
(or a registration regression) fails loudly.

The default expiry value itself has no e2e coverage on purpose: the regtest rig
always sets ``plugins.clink.invoice_expiry_sec`` explicitly (see the rig's
``electrum_clink_config_pairs``) so its liquidity-release waits stay fast, which
means the shipped default is only exercised here.
"""

from __future__ import annotations

import clink  # noqa: F401  (import registers the ConfigVars)
from electrum.simple_config import SimpleConfig


def test_invoice_expiry_default_is_300s() -> None:
    """The invoice-expiry / liquidity-lock window defaults to 300 seconds."""
    var = SimpleConfig.CLINK_INVOICE_EXPIRY
    assert var.get_default_value() == 300
    assert var.key() == "plugins.clink.invoice_expiry_sec"


def test_listener_robustness_defaults() -> None:
    """The silent-listener defense knobs ship with production cadences; the
    rig/e2e tests shrink them explicitly when they need fast recovery."""
    assert SimpleConfig.CLINK_WS_HEARTBEAT_SEC.get_default_value() == 30
    assert SimpleConfig.CLINK_WS_HEARTBEAT_SEC.key() == "plugins.clink.ws_heartbeat_sec"
    assert SimpleConfig.CLINK_WATCHDOG_INTERVAL_SEC.get_default_value() == 60
    assert SimpleConfig.CLINK_WATCHDOG_INTERVAL_SEC.key() == "plugins.clink.watchdog_interval_sec"
    assert SimpleConfig.CLINK_LISTENER_PING_INTERVAL_SEC.get_default_value() == 300
    assert (SimpleConfig.CLINK_LISTENER_PING_INTERVAL_SEC.key()
            == "plugins.clink.listener_ping_interval_sec")


def test_devfee_defaults_unchanged() -> None:
    """Companion pin: the dev-fee defaults the README documents."""
    assert SimpleConfig.CLINK_DEVFEE_ENABLED.get_default_value() is True
    assert SimpleConfig.CLINK_DEVFEE_RATE_PERCENT.get_default_value() == 0.1
    assert SimpleConfig.CLINK_DEVFEE_DEST.get_default_value() == "clink_fees@getbarebits.com"
