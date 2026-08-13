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
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterator

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


def _invoice_memo(bolt11: str) -> str:
    """Decode a regtest BOLT-11 invoice's memo.

    ``decode_bolt11_invoice`` defaults to ``constants.net`` (mainnet in a bare
    pytest process), which would mis-parse the ``lnbcrt`` HRP — so pin the net to
    regtest explicitly.
    """
    from electrum import constants
    from electrum.bolt11 import decode_bolt11_invoice
    return decode_bolt11_invoice(bolt11, net=constants.BitcoinRegtest).get_description()


def _fresh_noffer() -> str:
    created = json.loads(_electrum_cli("clink_add_offer", "--label", "e2e"))
    return created["noffer"]


def _fresh_noffer_memo_disallowed() -> str:
    created = json.loads(_electrum_cli("clink_add_offer", "--label", "e2e"))
    _electrum_cli("clink_set_offer_payer_memo", created["offer_id"], "false")
    offers = json.loads(_electrum_cli("clink_list_offers"))
    assert offers[created["offer_id"]]["allow_payer_memo"] is False
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


def test_created_offer_reports_payable_relay(rig) -> None:
    """Offer creation must run the payability probe and advertise a relay it
    verified — and that relay must actually round-trip an independent probe.

    This is the deterministic, offline-of-the-public-internet counterpart to the
    ``live_relay`` sweep: it proves the auto-pick + probe machinery end-to-end
    against the rig's own relay."""
    from clink.noffer import noffer_decode
    from clink.relay_probe import ProbeStatus, probe_relay_payable

    created = json.loads(_electrum_cli("clink_add_offer", "--label", "e2e"))
    assert created["relay_payable"] is True, created
    assert "warning" not in created, created

    # The relay the offer reports is exactly the one baked into its noffer...
    decoded = noffer_decode(created["noffer"])
    assert decoded.relay == created["relay"]

    # ...and an independent probe of that relay passes.
    result = asyncio.run(probe_relay_payable(decoded.relay, timeout=15))
    assert result.status is ProbeStatus.OK, result


def test_issued_invoice_carries_requested_description(rig) -> None:
    """The plugin folds the payer's NIP-69 ``description`` into the bolt11 memo,
    combined with the offer label as ``"<label> - <description>"``.

    The offer here is created with label ``e2e`` (see ``_fresh_noffer``), so a
    payer note of ``Acme Coffee - 2x Latte`` must surface as that combined memo
    on the minted regtest invoice (this is what cashupayserver relies on to put
    the store name in the customer's invoice)."""
    noffer = _fresh_noffer()
    available = _available_sat()
    amount = max(1, min(1000, available // 2))
    resp = asyncio.run(request_invoice(
        noffer, amount_sats=amount, description="Acme Coffee - 2x Latte", timeout=30))
    assert "bolt11" in resp, resp
    assert _invoice_memo(resp["bolt11"]) == "e2e - Acme Coffee - 2x Latte"


def test_disallowed_payer_memo_is_ignored(rig) -> None:
    """An offer with payer memos disallowed ignores the request ``description``;
    the issued invoice carries only the merchant's label."""
    noffer = _fresh_noffer_memo_disallowed()
    available = _available_sat()
    amount = max(1, min(1000, available // 2))
    resp = asyncio.run(request_invoice(
        noffer, amount_sats=amount, description="Acme Coffee - 2x Latte", timeout=30))
    assert "bolt11" in resp, resp
    assert _invoice_memo(resp["bolt11"]) == "e2e"


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


def test_error_range_does_not_reveal_exact_capacity(rig) -> None:
    """The invalid-amount range max must be quantized (liquidity-oracle fix):
    it never exceeds the true capacity and is stable under re-quantization,
    i.e. it carries at most two significant figures."""
    from clink.protocol import quantize_available_sat

    noffer = _fresh_noffer()
    available = _available_sat()
    resp = asyncio.run(request_invoice(noffer, amount_sats=available + 1_000_000, timeout=30))
    assert resp.get("code") == 5, resp
    reported = resp["range"]["max"]
    assert reported <= available + 1
    assert quantize_available_sat(reported) == reported


def test_malformed_offer_field_gets_clean_error(rig) -> None:
    """A request whose ``offer`` is a JSON array must be answered with a clean
    Invalid Offer error — regression for the unhashable-lookup TypeError that
    used to kill the dispatch (and send nothing) before the security review."""
    noffer = _fresh_noffer()
    resp = asyncio.run(request_invoice(
        noffer, amount_sats=None, timeout=30,
        payload_override={"offer": ["not-a-string"], "amount_sats": 5}))
    assert resp.get("code") == 1, resp


def test_bool_amount_gets_invalid_amount_error(rig) -> None:
    """JSON ``true`` as the amount must not mint a 1-sat invoice."""
    from clink.noffer import noffer_decode

    noffer = _fresh_noffer()
    offer_id = noffer_decode(noffer).offer
    resp = asyncio.run(request_invoice(
        noffer, amount_sats=None, timeout=30,
        payload_override={"offer": offer_id, "amount_sats": True}))
    assert resp.get("code") == 5, resp


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


def test_check_noffers_round_trips_and_leaves_no_trace(rig) -> None:
    """"Check noffers" (clink_check_noffers) self-tests offers end to end.

    Two fresh offers must both come back ``ok`` with a measured round-trip time
    through the rig's real relay and the plugin's live listener — and the check
    must leave nothing behind in the wallet: no liquidity reservation, no owed
    receipt, no payment request. The single-offer form checks exactly one.

    (The unreachable-relay failure path lives in the rig repo's own e2e suite,
    which boots a private stack it can kill the relay of; here the rig fixture
    is shared with the other tests, so we keep the relay alive.)
    """
    # The locking test just before this one may still hold (all of) the inbound
    # liquidity; the self-test requests 1 sat, so wait for that much to free up
    # (reservations expire after the 120s invoice-expiry window).
    deadline = time.monotonic() + 150
    while _available_sat() < 1 and time.monotonic() < deadline:
        time.sleep(3)
    assert _available_sat() >= 1, "no inbound liquidity freed up for the self-test"

    first = json.loads(_electrum_cli("clink_add_offer", "--label", "check-e2e"))
    second = json.loads(_electrum_cli("clink_add_offer", "--label", "check-e2e"))
    status_before = json.loads(_electrum_cli("clink_clink_status"))

    results = json.loads(_electrum_cli("clink_check_noffers"))
    for created in (first, second):
        res = results[created["offer_id"]]
        assert res["ok"] is True and res["status"] == "ok", res
        assert res["rtt_ms"] is not None and res["rtt_ms"] > 0, res
        assert res["noffer"] == created["noffer"], res
        assert res["checked_at"] > 0, res

    # No trace left behind: no new reservation or owed receipt (<= because a
    # leftover reservation from an earlier test may *expire* while we check),
    # and no wallet payment request bearing the self-test offers' label.
    status_after = json.loads(_electrum_cli("clink_clink_status"))
    assert status_after["active_reservations"] <= status_before["active_reservations"], status_after
    assert status_after["owed_receipts"] <= status_before["owed_receipts"], status_after
    # list_requests is wallet-scoped: unlike the clink_* plugin commands it
    # needs -w (this harness does not pin one the way the rig's helper does).
    wallet = RIG_DIR / ".run" / "electrum" / "regtest" / "wallets" / "clink_test"
    requests = json.loads(_electrum_cli("-w", str(wallet), "list_requests"))
    assert not any("check-e2e" in (r.get("message") or "") for r in requests), \
        "self-test left a payment request behind"

    # Single-offer form checks exactly that offer.
    single = json.loads(_electrum_cli(
        "clink_check_noffers", "--offer_id", first["offer_id"]))
    assert list(single) == [first["offer_id"]]
    assert single[first["offer_id"]]["ok"] is True


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_available_sat(minimum: int, timeout: float = 150.0) -> int:
    """Wait for reservations from earlier tests to expire and free liquidity."""
    deadline = time.monotonic() + timeout
    while _available_sat() < minimum and time.monotonic() < deadline:
        time.sleep(3)
    available = _available_sat()
    assert available >= minimum, f"inbound liquidity did not free up ({available} sat)"
    return available


def _start_extra_relay() -> "tuple[str, subprocess.Popen]":
    """Boot an extra in-rig Nostr relay on its own port; return (url, proc)."""
    port = _free_port()
    proc = subprocess.Popen(
        [str(RIG_PYTHON), "-m", "rig.relay", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(RIG_DIR),
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("extra relay exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.5)
    else:
        proc.terminate()
        raise TimeoutError("extra relay did not start listening")
    return f"ws://127.0.0.1:{port}", proc


def _stop_relay(proc: "subprocess.Popen") -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture()
def second_relay(rig) -> Iterator[str]:
    """A *second* in-rig Nostr relay on its own port.

    The rig's primary relay is injected as CLINK_RELAY, so a custom relay that
    differs from it is exactly what exercises the multi-relay listener union.
    """
    url, proc = _start_extra_relay()
    try:
        yield url
    finally:
        _stop_relay(proc)


def test_custom_relay_offer_round_trips(rig, second_relay) -> None:
    """An offer pinned to a user-chosen relay (the "type your own wss://…" path)
    must advertise that relay in its noffer, survive the payability probe, show
    up in list_offers with the custom relay — and actually be payable through
    it, which proves the listener joined the second relay alongside the rig's
    primary one."""
    from clink.noffer import noffer_decode

    created = json.loads(_electrum_cli(
        "clink_add_offer", "--label", "custom-relay-e2e", "--relay", second_relay))
    assert created["relay"] == second_relay, created
    assert created["relay_payable"] is True, created
    assert noffer_decode(created["noffer"]).relay == second_relay

    listed = json.loads(_electrum_cli("clink_list_offers"))[created["offer_id"]]
    assert listed["relay"] == second_relay
    assert listed["relay_custom"] is True
    assert listed["noffer"] == created["noffer"]

    # Creating the offer restarts the listener so it also sits on the custom
    # relay; the self-test round-trips through exactly that relay, so poll it
    # until the reconnect settles.
    _wait_available_sat(1)
    deadline = time.monotonic() + 60
    result: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        result = json.loads(_electrum_cli(
            "clink_check_noffers", "--offer_id", created["offer_id"]))[created["offer_id"]]
        if result["ok"]:
            break
        time.sleep(3)
    assert result.get("ok") is True, f"offer never became payable on its custom relay: {result}"

    # And a real payer (which connects only to the noffer's relay) gets an invoice.
    available = _available_sat()
    amount = max(1, min(1000, available // 2))
    resp = asyncio.run(request_invoice(created["noffer"], amount_sats=amount, timeout=30))
    assert "bolt11" in resp, resp

    # An auto-relay offer created afterwards still uses the rig's primary relay.
    auto = json.loads(_electrum_cli("clink_add_offer", "--label", "auto-after-custom"))
    assert noffer_decode(auto["noffer"]).relay != second_relay


def test_relay_liveness_reports_advertised_relay_payable(rig) -> None:
    """clink_check_relays (the on-demand form of the hourly liveness check)
    probes exactly the relays existing noffers advertise. With an offer on the
    rig's live relay, that relay must report ok on the first probe.

    Earlier tests may have left offers advertising relays that are gone (the
    custom-relay test's second relay dies with its fixture) — the sweep probes
    those too, so keep the retry delay short and assert only on our relay.
    """
    from clink.noffer import noffer_decode

    created = json.loads(_electrum_cli("clink_add_offer", "--label", "liveness-e2e"))
    advertised = noffer_decode(created["noffer"]).relay

    results = json.loads(_electrum_cli("clink_check_relays", "--retry_delay", "1"))
    assert advertised in results, results
    res = results[advertised]
    assert res["ok"] is True and res["status"] == "ok", res
    assert res["retried"] is False, res       # healthy relay: no confirming re-probe
    assert res["rtt_ms"] is not None and res["rtt_ms"] > 0, res
    assert res["checked_at"] > 0, res


def test_relay_liveness_flags_dead_relay_and_prunes_after_removal(rig) -> None:
    """A relay that dies *after* an offer advertised it must be flagged by the
    liveness check (after its single confirming re-probe), while the rig's live
    relay keeps reporting ok. Removing the offer prunes the dead relay."""
    primary = f"ws://127.0.0.1:{rig['clink_relay']}"
    relay_url, relay_proc = _start_extra_relay()
    created = None
    try:
        # An auto offer guarantees the primary relay is advertised (and thus
        # swept) regardless of which other tests ran before this one.
        auto = json.loads(_electrum_cli("clink_add_offer", "--label", "liveness-auto-e2e"))
        assert auto["relay"] == primary, auto
        created = json.loads(_electrum_cli(
            "clink_add_offer", "--label", "liveness-dead-e2e", "--relay", relay_url))
        assert created["relay_payable"] is True, created

        # The relay dies after the noffer went out — the exact silent failure
        # the periodic check exists to catch.
        _stop_relay(relay_proc)

        results = json.loads(_electrum_cli(
            "clink_check_relays", "--retry_delay", "1"))
        res = results[relay_url]
        assert res["ok"] is False, res
        assert res["retried"] is True, res    # verdict came from the re-probe
        assert res["status"] in ("unreachable", "no_readback", "error"), res

        # Offers on the rig's primary relay are unaffected.
        assert results[primary]["ok"] is True, results
    finally:
        _stop_relay(relay_proc)
        if created:
            _electrum_cli("clink_remove_offer", created["offer_id"])

    # With the offer gone nothing advertises the dead relay any more, so the
    # next sweep no longer probes (or reports) it.
    results = json.loads(_electrum_cli("clink_check_relays", "--retry_delay", "1"))
    assert relay_url not in results, results


def test_unreachable_custom_relay_blocks_creation(rig) -> None:
    """A custom relay that fails the payability probe must block creation
    (unlike the automatic path, which creates the offer with a warning)."""
    before = json.loads(_electrum_cli("clink_list_offers"))
    with pytest.raises(RuntimeError, match="payability"):
        _electrum_cli("clink_add_offer", "--label", "bad-relay-e2e",
                      "--relay", "ws://127.0.0.1:1")
    after = json.loads(_electrum_cli("clink_list_offers"))
    assert after.keys() == before.keys(), "a failing custom relay still created an offer"


def test_malformed_custom_relay_is_rejected(rig) -> None:
    before = json.loads(_electrum_cli("clink_list_offers"))
    with pytest.raises(RuntimeError, match="wss://"):
        _electrum_cli("clink_add_offer", "--relay", "https://not-a-websocket.example")
    after = json.loads(_electrum_cli("clink_list_offers"))
    assert after.keys() == before.keys()


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
