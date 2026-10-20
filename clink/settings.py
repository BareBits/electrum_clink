"""Per-wallet plugin settings with global-config fallback.

Every CLINK knob can be set *per wallet*; the value is persisted in the
wallet's own database (the same plugin storage that holds the wallet's offers,
dev-fee ledger and receipts), so two wallets open at the same time can run with
different relays, expiry windows and dev-fee choices.

A knob that was never set on a wallet falls back to the matching global
``SimpleConfig`` variable (``plugins.clink.*`` in Electrum's config file).
That keeps single-wallet setups, the regtest rig's config injection, and
pre-multi-wallet installs working unchanged: the globals act as defaults, a
wallet-level value overrides them.

Special case ``enabled``: CLINK service is opt-in per wallet. A wallet with no
explicit flag is treated as enabled when its storage already holds offers
(an install that was serving offers before activation became opt-in must keep
serving them), otherwise the global default applies.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, MutableMapping, Union

SettingValue = Union[str, int, float, bool]

# Key inside the wallet's plugin storage holding the override mapping.
STORAGE_KEY = "settings"
# Key the offer store persists under (see clink.offers.OfferStore.STORAGE_KEY);
# read here for the enabled-migration rule without importing the store.
_OFFERS_KEY = "offers"

# knob name -> (type, global SimpleConfig attribute used as the fallback)
KNOBS: Dict[str, tuple] = {
    "enabled": (bool, "CLINK_WALLET_DEFAULT_ENABLED"),
    "relay": (str, "CLINK_RELAY"),
    "invoice_expiry_sec": (int, "CLINK_INVOICE_EXPIRY"),
    "ws_heartbeat_sec": (int, "CLINK_WS_HEARTBEAT_SEC"),
    "watchdog_interval_sec": (int, "CLINK_WATCHDOG_INTERVAL_SEC"),
    "listener_ping_interval_sec": (int, "CLINK_LISTENER_PING_INTERVAL_SEC"),
    "devfee_enabled": (bool, "CLINK_DEVFEE_ENABLED"),
    "devfee_rate_percent": (float, "CLINK_DEVFEE_RATE_PERCENT"),
    "devfee_dest": (str, "CLINK_DEVFEE_DEST"),
    "devfee_notice_shown": (bool, "CLINK_DEVFEE_NOTICE_SHOWN"),
}

_TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
_FALSE_WORDS = frozenset({"false", "0", "no", "off"})


def coerce(name: str, raw: Union[SettingValue, None]) -> SettingValue:
    """Coerce ``raw`` (e.g. a CLI string) into ``name``'s proper type.

    Raises ``ValueError`` for an unknown knob or an uncoercible value.
    """
    if name not in KNOBS:
        raise ValueError(f"unknown CLINK setting: {name!r} "
                         f"(known: {', '.join(sorted(KNOBS))})")
    type_, _ = KNOBS[name]
    if type_ is bool:
        if isinstance(raw, bool):
            return raw
        word = str(raw).strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        raise ValueError(f"{name} expects true/false, got {raw!r}")
    if isinstance(raw, bool):  # bool is an int subclass; refuse the footgun
        raise ValueError(f"{name} expects {type_.__name__}, got a bool")
    try:
        return type_(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} expects {type_.__name__}, got {raw!r}") from e


class WalletSettings:
    """Typed accessors over one wallet's CLINK settings.

    ``storage`` is the wallet's plugin-storage mapping (persisted inside the
    wallet DB); ``config`` is Electrum's ``SimpleConfig`` (or any object
    carrying the ``CLINK_*`` attributes) supplying the global fallbacks.

    Reads always hit the storage mapping afresh, so any number of instances
    over the same wallet agree without cross-invalidation.
    """

    def __init__(self, storage: MutableMapping[str, Any], config: Any) -> None:
        self._storage = storage
        self._config = config

    # -- generic access -----------------------------------------------------

    def _overrides(self) -> Mapping[str, Any]:
        overrides = self._storage.get(STORAGE_KEY)
        return overrides if isinstance(overrides, Mapping) else {}

    def _write(self, overrides: Dict[str, Any]) -> None:
        # Whole-value assignment (not in-place mutation) so Electrum's
        # StoredDict change tracking persists the write to the wallet file.
        self._storage[STORAGE_KEY] = overrides

    def is_overridden(self, name: str) -> bool:
        if name not in KNOBS:
            raise ValueError(f"unknown CLINK setting: {name!r}")
        return name in self._overrides()

    def get(self, name: str) -> SettingValue:
        """The effective value: the wallet override, else the global config."""
        if name not in KNOBS:
            raise ValueError(f"unknown CLINK setting: {name!r}")
        overrides = self._overrides()
        if name in overrides:
            return coerce(name, overrides[name])
        if name == "enabled" and self._storage.get(_OFFERS_KEY):
            # Migration: a wallet that was already handing out offers before
            # activation became opt-in keeps serving them without any action.
            return True
        _, config_attr = KNOBS[name]
        return coerce(name, getattr(self._config, config_attr))

    def set(self, name: str, value: Union[SettingValue, None]) -> SettingValue:
        """Set the wallet-level override; returns the coerced value."""
        coerced = coerce(name, value)
        overrides = dict(self._overrides())
        overrides[name] = coerced
        self._write(overrides)
        return coerced

    def clear(self, name: str) -> None:
        """Drop the wallet-level override; the knob falls back to the global."""
        if name not in KNOBS:
            raise ValueError(f"unknown CLINK setting: {name!r}")
        overrides = dict(self._overrides())
        if name in overrides:
            del overrides[name]
            self._write(overrides)

    def source(self, name: str) -> str:
        """Where the effective value comes from: ``"wallet"`` or ``"global"``."""
        return "wallet" if self.is_overridden(name) else "global"

    def effective(self) -> Dict[str, Dict[str, Any]]:
        """Every knob's effective value and source (for ``clink_settings``)."""
        return {name: {"value": self.get(name), "source": self.source(name)}
                for name in KNOBS}

    # -- typed properties ----------------------------------------------------

    def _bool(self, name: str) -> bool:
        return bool(self.get(name))

    @property
    def enabled(self) -> bool:
        return self._bool("enabled")

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self.set("enabled", bool(value))

    @property
    def relay(self) -> str:
        return str(self.get("relay"))

    @relay.setter
    def relay(self, value: str) -> None:
        self.set("relay", str(value))

    @property
    def invoice_expiry_sec(self) -> int:
        return int(self.get("invoice_expiry_sec"))

    @invoice_expiry_sec.setter
    def invoice_expiry_sec(self, value: int) -> None:
        self.set("invoice_expiry_sec", int(value))

    @property
    def ws_heartbeat_sec(self) -> int:
        return int(self.get("ws_heartbeat_sec"))

    @property
    def watchdog_interval_sec(self) -> int:
        return int(self.get("watchdog_interval_sec"))

    @property
    def listener_ping_interval_sec(self) -> int:
        return int(self.get("listener_ping_interval_sec"))

    @property
    def devfee_enabled(self) -> bool:
        return self._bool("devfee_enabled")

    @devfee_enabled.setter
    def devfee_enabled(self, value: bool) -> None:
        self.set("devfee_enabled", bool(value))

    @property
    def devfee_rate_percent(self) -> float:
        return float(self.get("devfee_rate_percent"))

    @devfee_rate_percent.setter
    def devfee_rate_percent(self, value: float) -> None:
        self.set("devfee_rate_percent", float(value))

    @property
    def devfee_dest(self) -> str:
        return str(self.get("devfee_dest"))

    @property
    def devfee_notice_shown(self) -> bool:
        # Falls back to the *global* flag so a pre-multi-wallet install that
        # already showed the notice does not nag once per wallet.
        return self._bool("devfee_notice_shown")

    @devfee_notice_shown.setter
    def devfee_notice_shown(self, value: bool) -> None:
        self.set("devfee_notice_shown", bool(value))
