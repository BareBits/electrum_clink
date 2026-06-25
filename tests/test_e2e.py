"""End-to-end tests for the CLINK plugin against the live regtest rig.

Boots the sibling ``electrum-regtest-rig`` (bitcoind + ElectrumX + LND + Electrum
with the CLINK plugin enabled + the in-rig Nostr relay, seeded with balanced
Lightning channels), then drives the full protocol from a real payer:

  * happy path        -> a payable BOLT-11 invoice is returned
  * over-capacity     -> error code 5 (Invalid Amount) with a range
  * liquidity locking -> an issued invoice reserves inbound liquidity, so an
                         immediate second request for the remaining capacity is
                         refused until the first expires

These are slow (full stack bring-up + seeding, ~2-3 min) and require the rig
checkout; the suite self-skips if it is absent. Run with: ``pytest -m e2e``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from tests.clink_payer import request_invoice, request_invoice_and_receipt

pytestmark = pytest.mark.e2e

RIG_DIR = Path(__file__).resolve().parents[2] / "electrum-regtest-rig"
RIG_PYTHON = RIG_DIR / ".venv-electrum" / "bin" / "python"
ELECTRUM_BIN = RIG_DIR / ".venv-electrum" / "bin" / "electrum"
BOOT_TIMEOUT = 300.0


def _rig_available() -> bool:
    return RIG_PYTHON.exists() and (RIG_DIR / "run.py").exists()


def _electrum_cli(*args: str) -> str:
    out = subprocess.run(
        [str(ELECTRUM_BIN), "--regtest", "--dir", str(RIG_DIR / ".run" / "electrum"), *args],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"electrum {args} failed: {out.stderr.strip()}")
    return out.stdout.strip()


@pytest.fixture(scope="module")
def rig() -> Dict[str, Any]:
    if not _rig_available():
        pytest.skip("electrum-regtest-rig not available")
    ready_file = Path("/tmp/clink-e2e-ready.json")
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
        # seeding must have produced inbound liquidity for the happy path
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


def _fresh_noffer() -> str:
    created = json.loads(_electrum_cli("clink_add_offer", "--label", "e2e"))
    return created["noffer"]


def _available_sat() -> int:
    return int(json.loads(_electrum_cli("clink_clink_status"))["available_sat"])


def _devfee_status() -> Dict[str, Any]:
    return json.loads(_electrum_cli("clink_devfee_status"))


def _lnd_pay(bolt11: str, lnd_grpc: int) -> None:
    """Pay a BOLT-11 invoice from the rig's LND (the wallet's counterparty)."""
    script = (
        "import sys, json;"
        "from rig.services import Endpoints;"
        "from rig.lnd import lnd_pay_invoice;"
        f"ep = Endpoints(btc_rpc=0, electrumx_tcp=0, electrumx_rpc=0, lnd_grpc={lnd_grpc});"
        "lnd_pay_invoice(ep, sys.argv[1])"
    )
    out = subprocess.run(
        [str(RIG_PYTHON), "-c", script, bolt11],
        cwd=str(RIG_DIR), capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"lnd pay failed: {out.stderr.strip()}")


def test_happy_path_returns_payable_invoice(rig) -> None:
    noffer = _fresh_noffer()
    available = _available_sat()
    assert available > 0, "rig wallet should have inbound liquidity after seeding"
    amount = max(1, min(1000, available // 2))
    resp = asyncio.run(request_invoice(noffer, amount_sats=amount, timeout=30))
    assert "bolt11" in resp, resp
    assert resp["bolt11"].lower().startswith("lnbcrt")


def test_payment_receipt_delivered_after_payment(rig) -> None:
    # Full round trip: request -> invoice -> pay it from LND -> the plugin should
    # send the payer a kind-21001 {"res":"ok"} receipt on the same subscription.
    noffer = _fresh_noffer()
    available = _available_sat()
    assert available > 0, "rig wallet should have inbound liquidity after seeding"
    amount = max(1, min(1000, available // 2))
    result = asyncio.run(request_invoice_and_receipt(
        noffer, amount_sats=amount,
        pay=lambda bolt11: _lnd_pay(bolt11, rig["lnd_grpc"]),
        timeout=90,
    ))
    assert "bolt11" in result["invoice"], result
    assert result["invoice"]["bolt11"].lower().startswith("lnbcrt")
    assert result["receipt"] == {"res": "ok"}, result


def test_over_capacity_returns_error_5(rig) -> None:
    noffer = _fresh_noffer()
    available = _available_sat()
    resp = asyncio.run(request_invoice(noffer, amount_sats=available + 1_000_000, timeout=30))
    assert resp.get("code") == 5, resp
    assert resp["range"]["max"] <= available + 1


def test_issued_invoice_locks_liquidity(rig) -> None:
    noffer = _fresh_noffer()
    available = _available_sat()
    assert available > 2, "need some capacity to split"
    # First request takes (almost) all capacity and holds it via the unpaid invoice.
    first = asyncio.run(request_invoice(noffer, amount_sats=available, timeout=30))
    assert "bolt11" in first, first
    # Reservation should now show up and shrink availability.
    assert _available_sat() < available
    # A second request for what *was* available must now be refused (error 5),
    # proving the first invoice locked the inbound liquidity.
    second = asyncio.run(request_invoice(noffer, amount_sats=available, timeout=30))
    assert second.get("code") == 5, second


def test_devfee_accrues_and_pays_out(rig) -> None:
    # Exercise the real default 0.1% rate. To cross the 1,000-sat payout
    # threshold we receive ~1.05M sat (0.1% -> ~1,050 sat). The seeded channels
    # (0.15 + 0.10 BTC, pushed half each) leave several million sat of inbound,
    # so this fits in a single channel. The rig points the dev-fee destination
    # at its in-rig LNURL payee (backed by LND).
    pay_amount = 1_050_000
    expected_fee = pay_amount // 1000  # 0.1% of pay_amount, floored
    assert expected_fee >= 1_000

    # Wait for inbound liquidity to recover from the locking test's reservations.
    deadline = time.monotonic() + 150
    while _available_sat() < pay_amount and time.monotonic() < deadline:
        time.sleep(3)
    available = _available_sat()
    if available < pay_amount:
        pytest.skip(f"not enough inbound liquidity to test dev fee ({available} sat)")

    status = _devfee_status()
    assert status["rate_percent"] == pytest.approx(0.1), status
    owed_before = status["owed_sat"]

    noffer = _fresh_noffer()
    resp = asyncio.run(request_invoice(noffer, amount_sats=pay_amount, timeout=30))
    assert "bolt11" in resp, resp

    # Pay the offer invoice from LND -> the wallet receives it -> the dev fee
    # accrues -> a payout is auto-triggered (last_attempt was never stamped, so
    # the once-a-day gate is open).
    _lnd_pay(resp["bolt11"], rig["lnd_grpc"])

    # The accrual + payout are async; poll until the dev fee is forwarded.
    deadline = time.monotonic() + 90
    forced = False
    while time.monotonic() < deadline:
        status = _devfee_status()
        if status["paid_last_24h_sat"] >= 1_000:
            break
        # If the fee accrued but the auto-payout hasn't fired yet, nudge it once.
        if not forced and status["owed_sat"] >= 1_000:
            _electrum_cli("clink_devfee_pay")
            forced = True
        time.sleep(3)

    assert status["paid_last_24h_sat"] >= 1_000, f"dev fee was not paid out: {status}"
    # The payout was within the 10,000 sat/day cap and debited the owed balance.
    assert status["paid_last_24h_sat"] <= 10_000, status
    assert status["owed_sat"] < owed_before + expected_fee, status
