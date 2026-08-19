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


def test_devfee_defaults_unchanged() -> None:
    """Companion pin: the dev-fee defaults the README documents."""
    assert SimpleConfig.CLINK_DEVFEE_ENABLED.get_default_value() is True
    assert SimpleConfig.CLINK_DEVFEE_RATE_PERCENT.get_default_value() == 0.1
    assert SimpleConfig.CLINK_DEVFEE_DEST.get_default_value() == "clink_fees@getbarebits.com"
