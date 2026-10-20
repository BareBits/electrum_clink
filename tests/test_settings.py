"""Unit tests for the per-wallet settings layer (clink.settings).

Pins the three-level resolution every knob follows — wallet override, else
global config, plus the ``enabled`` migration rule — and the string coercion
the clink_set_setting command relies on.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict

import pytest

from clink.settings import KNOBS, STORAGE_KEY, WalletSettings, coerce


def _config(**overrides: Any) -> SimpleNamespace:
    values: Dict[str, Any] = dict(
        CLINK_WALLET_DEFAULT_ENABLED=False,
        CLINK_RELAY="wss://global.example",
        CLINK_INVOICE_EXPIRY=300,
        CLINK_WS_HEARTBEAT_SEC=30,
        CLINK_WATCHDOG_INTERVAL_SEC=60,
        CLINK_LISTENER_PING_INTERVAL_SEC=300,
        CLINK_DEVFEE_ENABLED=True,
        CLINK_DEVFEE_RATE_PERCENT=0.1,
        CLINK_DEVFEE_DEST="clink_fees@getbarebits.com",
        CLINK_DEVFEE_NOTICE_SHOWN=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_unset_knobs_fall_back_to_global_config() -> None:
    ws = WalletSettings({}, _config())
    assert ws.relay == "wss://global.example"
    assert ws.invoice_expiry_sec == 300
    assert ws.devfee_enabled is True
    assert ws.devfee_rate_percent == pytest.approx(0.1)
    assert ws.devfee_dest == "clink_fees@getbarebits.com"
    assert all(ws.source(name) == "global" for name in KNOBS)


def test_wallet_override_beats_global_and_persists() -> None:
    storage: Dict[str, Any] = {}
    ws = WalletSettings(storage, _config())
    ws.relay = "wss://mine.example"
    ws.invoice_expiry_sec = 60
    assert ws.relay == "wss://mine.example"
    assert ws.invoice_expiry_sec == 60
    assert ws.source("relay") == "wallet"
    # Persisted into the wallet's plugin storage, where a fresh instance
    # (e.g. another WalletSettings over the same wallet) sees it.
    assert storage[STORAGE_KEY]["relay"] == "wss://mine.example"
    assert WalletSettings(storage, _config()).relay == "wss://mine.example"


def test_two_wallets_do_not_share_overrides() -> None:
    config = _config()
    ws_a = WalletSettings({}, config)
    ws_b = WalletSettings({}, config)
    ws_a.invoice_expiry_sec = 60
    assert ws_a.invoice_expiry_sec == 60
    assert ws_b.invoice_expiry_sec == 300  # untouched wallet keeps the global


def test_clear_restores_the_global_fallback() -> None:
    storage: Dict[str, Any] = {}
    ws = WalletSettings(storage, _config())
    ws.set("invoice_expiry_sec", 60)
    ws.clear("invoice_expiry_sec")
    assert ws.invoice_expiry_sec == 300
    assert ws.source("invoice_expiry_sec") == "global"
    ws.clear("invoice_expiry_sec")  # idempotent


def test_writes_reassign_whole_mapping_for_storeddict_tracking() -> None:
    """Electrum's StoredDict only reliably persists whole-value assignment;
    in-place mutation of a nested plain dict can be missed. Pin the pattern."""

    class RecordingDict(dict):
        def __init__(self) -> None:
            super().__init__()
            self.assignments = 0

        def __setitem__(self, key: str, value: Any) -> None:
            self.assignments += 1
            super().__setitem__(key, value)

    storage = RecordingDict()
    ws = WalletSettings(storage, _config())
    ws.set("relay", "wss://a.example")
    ws.set("invoice_expiry_sec", 60)
    ws.clear("relay")
    assert storage.assignments == 3


def test_set_coerces_cli_strings() -> None:
    ws = WalletSettings({}, _config())
    assert ws.set("invoice_expiry_sec", "45") == 45
    assert ws.invoice_expiry_sec == 45
    assert ws.set("devfee_rate_percent", "0.5") == pytest.approx(0.5)
    assert ws.set("devfee_enabled", "false") is False
    assert ws.set("devfee_enabled", "True") is True
    assert ws.set("enabled", "on") is True


def test_unknown_key_and_bad_values_are_rejected() -> None:
    ws = WalletSettings({}, _config())
    with pytest.raises(ValueError, match="unknown CLINK setting"):
        ws.set("nonsense", "1")
    with pytest.raises(ValueError, match="unknown CLINK setting"):
        ws.get("nonsense")
    with pytest.raises(ValueError, match="true/false"):
        ws.set("devfee_enabled", "maybe")
    with pytest.raises(ValueError, match="int"):
        ws.set("invoice_expiry_sec", "soon")
    with pytest.raises(ValueError):
        coerce("invoice_expiry_sec", True)  # a bool is not an int here


def test_effective_reports_value_and_source_for_every_knob() -> None:
    ws = WalletSettings({}, _config())
    ws.set("relay", "wss://mine.example")
    eff = ws.effective()
    assert set(eff) == set(KNOBS)
    assert eff["relay"] == {"value": "wss://mine.example", "source": "wallet"}
    assert eff["invoice_expiry_sec"] == {"value": 300, "source": "global"}


# --- the enabled flag's special resolution ------------------------------------

def test_enabled_defaults_to_global_flag() -> None:
    assert WalletSettings({}, _config()).enabled is False
    assert WalletSettings({}, _config(CLINK_WALLET_DEFAULT_ENABLED=True)).enabled is True


def test_enabled_explicit_override_wins() -> None:
    storage: Dict[str, Any] = {}
    ws = WalletSettings(storage, _config())
    ws.enabled = True
    assert ws.enabled is True
    ws.enabled = False
    assert WalletSettings(storage, _config(CLINK_WALLET_DEFAULT_ENABLED=True)).enabled is False


def test_enabled_grandfathers_wallets_with_offers() -> None:
    """A wallet already holding offers (pre-opt-in install) counts as enabled
    unless explicitly opted out."""
    storage: Dict[str, Any] = {"offers": [{"offer_id": "o1"}]}
    assert WalletSettings(storage, _config()).enabled is True
    storage[STORAGE_KEY] = {"enabled": False}
    assert WalletSettings(storage, _config()).enabled is False


def test_devfee_notice_falls_back_to_global_flag() -> None:
    """An install that saw the first-run notice before settings became
    per-wallet (global flag true) is not nagged once per wallet."""
    assert WalletSettings({}, _config(CLINK_DEVFEE_NOTICE_SHOWN=True)).devfee_notice_shown is True
    ws = WalletSettings({}, _config())
    assert ws.devfee_notice_shown is False
    ws.devfee_notice_shown = True
    assert ws.devfee_notice_shown is True
