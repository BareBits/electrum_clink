"""End-to-end tests: several wallets serving CLINK offers at the same time.

Boots the regtest rig, then loads a *second* lightning wallet into the same
Electrum daemon and gives it its own channel (and thus inbound liquidity) to
the rig's LND. Both wallets then serve CLINK concurrently, which verifies:

  * each wallet runs its own server with its own Nostr identity, and each
    wallet's noffers are reachable/payable end to end — concurrently
  * offers and liquidity are wallet-scoped (no cross-contamination)
  * per-wallet settings govern only their wallet: a relay and invoice-expiry
    override on wallet 2 changes wallet 2's noffers/invoices while wallet 1
    keeps the rig's global config
  * disabling CLINK on one wallet silences only that wallet

Slow (full stack bring-up + an extra funded channel); requires the rig
checkout and self-skips without it. Run with: ``pytest -m e2e``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import pytest

from tests.clink_payer import request_invoice
from tests.test_e2e import (
    BOOT_TIMEOUT,
    RIG_DIR,
    RIG_PYTHON,
    SERVICE_WALLET,
    _electrum_cli,
    _lnd_pay,
    _rig_available,
    _start_extra_relay,
    _stop_relay,
)

pytestmark = pytest.mark.e2e

# The second service wallet, created by this module inside the rig daemon's
# own datadir (so one daemon serves both wallets — the multi-wallet case).
WALLET2 = RIG_DIR / ".run" / "electrum" / "regtest" / "wallets" / "clink_multi2"
# wallet2's channel to LND: (capacity, push) — push half for inbound liquidity.
WALLET2_FUNDING_BTC = "0.1"
WALLET2_CHANNEL = ("0.05", "0.025")


@pytest.fixture(scope="module")
def rig() -> Iterator[Dict[str, Any]]:
    if not _rig_available():
        pytest.skip("electrum-regtest-rig not available")
    ready_file = Path("/tmp/clink-e2e-multiwallet-ready.json")
    ready_file.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [str(RIG_PYTHON), "run.py", "--no-gui", "--ready-file", str(ready_file)],
        cwd=str(RIG_DIR), start_new_session=True,
    )
    try:
        deadline = time.monotonic() + BOOT_TIMEOUT
        while time.monotonic() < deadline:
            if ready_file.exists():
                break
            if proc.poll() is not None:
                raise RuntimeError("rig exited before becoming ready")
            time.sleep(2)
        else:
            raise TimeoutError("rig did not become ready in time")
        info = json.loads(ready_file.read_text())
        assert info.get("seeded", {}).get("channels", 0) >= 1
        yield info
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            proc.wait(timeout=30)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


# --------------------------------------------------------------------------
# second-wallet bring-up helpers
# --------------------------------------------------------------------------
def _rig_py(script: str, *argv: str) -> str:
    out = subprocess.run(
        [str(RIG_PYTHON), "-c", script, *argv],
        cwd=str(RIG_DIR), capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"rig python failed: {out.stderr.strip()}")
    return out.stdout.strip()


def _mine(rig: Dict[str, Any], blocks: int) -> None:
    """Mine ``blocks`` to the rig's service address via the rig's bitcoin-cli."""
    script = (
        "import sys;"
        "from rig.services import Endpoints, bitcoin_cli;"
        f"ep = Endpoints(btc_rpc={rig['bitcoind_rpc']}, electrumx_tcp=0, electrumx_rpc=0);"
        "bitcoin_cli(ep, 'generatetoaddress', sys.argv[1], sys.argv[2])"
    )
    _rig_py(script, str(blocks), rig["address"])


def _lnd_node_uri(rig: Dict[str, Any]) -> str:
    script = (
        "from rig.services import Endpoints;"
        "from rig.lnd import lnd_pubkey;"
        f"ep = Endpoints(btc_rpc=0, electrumx_tcp=0, electrumx_rpc=0, lnd_grpc={rig['lnd_grpc']});"
        "print(lnd_pubkey(ep))"
    )
    pubkey = _rig_py(script).splitlines()[-1].strip()
    return f"{pubkey}@127.0.0.1:{rig['lnd_p2p']}"


def _extract_tx_hex(payto_output: str) -> str:
    payto_output = payto_output.strip()
    try:
        parsed = json.loads(payto_output)
    except json.JSONDecodeError:
        return payto_output
    if isinstance(parsed, dict):
        return str(parsed.get("hex") or parsed.get("tx") or payto_output)
    return str(parsed)


def _wait(predicate, *, timeout: float, desc: str, poll: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # transient CLI/daemon hiccup; keep polling
            last_exc = exc
        time.sleep(poll)
    suffix = f" (last error: {last_exc})" if last_exc else ""
    raise TimeoutError(f"timed out waiting for {desc}{suffix}")


def _confirmed_btc(wallet: Path) -> float:
    balance = json.loads(_electrum_cli("getbalance", wallet=wallet))
    return float(balance.get("confirmed", 0))


def _available_sat(wallet: Optional[Path] = None) -> int:
    return int(json.loads(
        _electrum_cli("clink_clink_status", wallet=wallet))["available_sat"])


def _channels(wallet: Path) -> list:
    return list(json.loads(_electrum_cli("list_channels", wallet=wallet)))


@pytest.fixture(scope="module")
def wallet2(rig) -> Iterator[Path]:
    """Create, load, fund and channel the second wallet inside the rig daemon.

    The rig config sets ``plugins.clink.wallet_default_enabled=true``, so the
    plugin's ``daemon_wallet_loaded`` hook starts wallet 2's CLINK server the
    moment it loads — before it even has liquidity, like a fresh real wallet.
    """
    # No -o: with the rig's daemon holding the datadir lock, create must route
    # through the daemon (offline create refuses to run beside it).
    _electrum_cli("create", wallet=WALLET2)
    _electrum_cli("load_wallet", wallet=WALLET2)

    # Fund wallet 2 on-chain from the (seeded) service wallet.
    addr2 = _electrum_cli("getunusedaddress", wallet=WALLET2)
    tx_hex = _extract_tx_hex(_electrum_cli("payto", addr2, WALLET2_FUNDING_BTC))
    _electrum_cli("broadcast", tx_hex)
    _mine(rig, 1)
    _wait(lambda: _confirmed_btc(WALLET2) >= float(WALLET2_FUNDING_BTC) - 0.001,
          timeout=60, desc="wallet2 to see its on-chain funding")

    # Open a balanced channel wallet2 -> LND (push half => inbound liquidity).
    capacity, push = WALLET2_CHANNEL
    _electrum_cli("open_channel", _lnd_node_uri(rig), capacity,
                  "--push_amount", push, "--password", "", wallet=WALLET2)
    _mine(rig, 6)
    _wait(lambda: _channels(WALLET2) and all(
        c["state"] == "OPEN" for c in _channels(WALLET2)),
          timeout=120, desc="wallet2 channel to reach OPEN")
    # Receivable capacity through the plugin proves wallet2's own CLINK
    # server is up and reading wallet2's (not wallet 1's) lightning state.
    _wait(lambda: _available_sat(WALLET2) > 0,
          timeout=60, desc="wallet2 inbound liquidity to register")
    yield WALLET2


def _add_offer(label: str, wallet: Optional[Path] = None, *extra: str) -> Dict[str, Any]:
    return json.loads(_electrum_cli(
        "clink_add_offer", "--label", label, *extra, wallet=wallet))


def _request_with_retry(noffer: str, amount_sats: int, *, attempts: int = 10,
                        timeout: int = 15, **kwargs: Any) -> Dict[str, Any]:
    """request_invoice, retried: a request published into a listener
    reconnect window (offer creation restarts the listener) can need a
    second try before the subscription is live again."""
    last: Dict[str, Any] = {}
    for _ in range(attempts):
        try:
            last = asyncio.run(request_invoice(
                noffer, amount_sats=amount_sats, timeout=timeout, **kwargs))
        except Exception as e:
            last = {"error": repr(e)}
        if "bolt11" in last:
            return last
        time.sleep(3)
    return last


def _invoice_expiry(bolt11: str) -> int:
    from electrum import constants
    from electrum.bolt11 import decode_bolt11_invoice
    return int(decode_bolt11_invoice(bolt11, net=constants.BitcoinRegtest).get_expiry())


# --------------------------------------------------------------------------
# the tests (ordered: the disable test runs last)
# --------------------------------------------------------------------------
def test_both_wallets_run_with_distinct_identities(rig, wallet2) -> None:
    """Each wallet's server derives its own Nostr identity, and offers land in
    the wallet they were created in — not in the other one."""
    from clink.noffer import noffer_decode

    offer1 = _add_offer("mw-wallet1")
    offer2 = _add_offer("mw-wallet2", WALLET2)

    assert noffer_decode(offer1["noffer"]).pubkey != noffer_decode(offer2["noffer"]).pubkey

    listed1 = json.loads(_electrum_cli("clink_list_offers"))
    listed2 = json.loads(_electrum_cli("clink_list_offers", wallet=WALLET2))
    assert offer1["offer_id"] in listed1 and offer1["offer_id"] not in listed2
    assert offer2["offer_id"] in listed2 and offer2["offer_id"] not in listed1


def test_both_wallets_noffers_are_payable_concurrently(rig, wallet2) -> None:
    """The core promise: two wallets, two listeners, one daemon — a payer gets
    a valid invoice from each wallet's noffer, requested at the same time, and
    each invoice is minted by its own wallet (check via its own self-test)."""
    offer1 = _add_offer("mw-pay1")
    offer2 = _add_offer("mw-pay2", WALLET2)

    async def both() -> list:
        return await asyncio.gather(
            request_invoice(offer1["noffer"], amount_sats=1000, timeout=30),
            request_invoice(offer2["noffer"], amount_sats=1000, timeout=30),
            return_exceptions=True,
        )

    # Offer creation can restart either listener; retry until both answer.
    deadline = time.monotonic() + 90
    resp1: Any = None
    resp2: Any = None
    while time.monotonic() < deadline:
        resp1, resp2 = asyncio.run(both())
        if (isinstance(resp1, dict) and "bolt11" in resp1
                and isinstance(resp2, dict) and "bolt11" in resp2):
            break
        time.sleep(3)
    assert isinstance(resp1, dict) and "bolt11" in resp1, resp1
    assert isinstance(resp2, dict) and "bolt11" in resp2, resp2

    # The plugin's own end-to-end self-test agrees, per wallet.
    checked1 = json.loads(_electrum_cli(
        "clink_check_noffers", "--offer_id", offer1["offer_id"]))
    checked2 = json.loads(_electrum_cli(
        "clink_check_noffers", "--offer_id", offer2["offer_id"], wallet=WALLET2))
    assert checked1[offer1["offer_id"]]["ok"] is True, checked1
    assert checked2[offer2["offer_id"]]["ok"] is True, checked2


def test_paying_wallet2_only_moves_wallet2_liquidity(rig, wallet2) -> None:
    """An invoice issued and paid on wallet 2 settles into wallet 2; wallet 1's
    balances are untouched (invoices and liquidity are wallet-scoped)."""
    def _ln_btc(wallet: Optional[Path] = None) -> float:
        return float(json.loads(_electrum_cli("getbalance", wallet=wallet))
                     .get("lightning", 0))

    ln1_before = _ln_btc()
    ln2_before = _ln_btc(WALLET2)
    offer2 = _add_offer("mw-isolation", WALLET2)
    resp = _request_with_retry(offer2["noffer"], 25_000)
    assert "bolt11" in resp, resp
    _lnd_pay(resp["bolt11"], rig["lnd_grpc"])
    _wait(lambda: _ln_btc(WALLET2) >= ln2_before + 0.0002,
          timeout=90, desc="wallet2 to receive the payment")
    # Wallet 1's lightning balance did not move by this payment.
    assert _ln_btc() == pytest.approx(ln1_before, abs=1e-8)


def test_per_wallet_settings_govern_only_their_wallet(rig, wallet2) -> None:
    """Wallet 2 overrides relay + invoice expiry; wallet 1 keeps the globals.

    The override must show up in wallet 2's noffer (its advertised relay) and
    in the invoices it mints (expiry), while wallet 1's stay on the rig
    defaults — two wallets, one daemon, different effective settings."""
    from clink.noffer import noffer_decode

    primary_relay = f"ws://127.0.0.1:{rig['clink_relay']}"
    relay_url, relay_proc = _start_extra_relay()
    try:
        set_result = json.loads(_electrum_cli(
            "clink_set_setting", "relay", "--value", relay_url, wallet=WALLET2))
        assert set_result["relay"] == {"value": relay_url, "source": "wallet"}
        json.loads(_electrum_cli(
            "clink_set_setting", "invoice_expiry_sec", "--value", "60", wallet=WALLET2))

        # The settings report shows the split: wallet-level on 2, global on 1.
        eff2 = json.loads(_electrum_cli("clink_get_settings", wallet=WALLET2))
        eff1 = json.loads(_electrum_cli("clink_get_settings"))
        assert eff2["relay"]["source"] == "wallet"
        assert eff2["invoice_expiry_sec"] == {"value": 60, "source": "wallet"}
        assert eff1["relay"] == {"value": primary_relay, "source": "global"}
        assert eff1["invoice_expiry_sec"] == {"value": 120, "source": "global"}

        # New offers advertise each wallet's own relay...
        offer2 = _add_offer("mw-settings2", WALLET2)
        offer1 = _add_offer("mw-settings1")
        assert noffer_decode(offer2["noffer"]).relay == relay_url, offer2
        assert noffer_decode(offer1["noffer"]).relay == primary_relay, offer1

        # ...and both stay payable (wallet 2 through its own relay), with each
        # wallet's own expiry on the minted invoice.
        resp2 = _request_with_retry(offer2["noffer"], 1000)
        assert "bolt11" in resp2, resp2
        assert _invoice_expiry(resp2["bolt11"]) == 60
        resp1 = _request_with_retry(offer1["noffer"], 1000)
        assert "bolt11" in resp1, resp1
        assert _invoice_expiry(resp1["bolt11"]) == 120
    finally:
        # Drop wallet 2's relay override (the extra relay dies with this test;
        # later offers must go back to the rig's primary relay). The expiry
        # override deliberately stays — later tests don't depend on it.
        _electrum_cli("clink_set_setting", "relay", "--unset", wallet=WALLET2)
        _stop_relay(relay_proc)


def test_disabling_one_wallet_silences_only_that_wallet(rig, wallet2) -> None:
    """clink_disable on wallet 2 stops its listener (its noffers go
    unanswered) while wallet 1 keeps serving; re-enabling brings wallet 2
    back. Runs last in this module: it bounces wallet 2's server."""
    offer1 = _add_offer("mw-disable1")
    offer2 = _add_offer("mw-disable2", WALLET2)
    # Both answering before the flip (also absorbs the listener restarts the
    # offer creation just triggered).
    assert "bolt11" in _request_with_retry(offer1["noffer"], 1000)
    assert "bolt11" in _request_with_retry(offer2["noffer"], 1000)

    status = json.loads(_electrum_cli("clink_disable", wallet=WALLET2))
    assert (status["enabled"], status["running"]) == (False, False), status
    assert status["wallet"].endswith(WALLET2.name), status

    # Wallet 2 is deaf now: a request gets no response (payer times out)...
    silent = _request_with_retry(offer2["noffer"], 1000, attempts=1, timeout=12)
    assert "bolt11" not in silent, silent
    # ...while wallet 1 still answers.
    assert "bolt11" in _request_with_retry(offer1["noffer"], 1000), "wallet1 went deaf too"

    status = json.loads(_electrum_cli("clink_enable", wallet=WALLET2))
    assert (status["enabled"], status["running"]) == (True, True), status
    assert "bolt11" in _request_with_retry(offer2["noffer"], 1000, attempts=20), \
        "wallet2 did not resume serving after re-enable"
