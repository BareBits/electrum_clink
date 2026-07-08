"""Live payability sweep of Electrum's default Nostr relay list.

This is the test that would have caught the reported bug: a default noffer
embeds ``NOSTR_RELAYS[0]``, and if that relay won't carry CLINK's ephemeral
kind-21001 traffic the offer is silently unpayable. Here we probe *every*
default relay for real and assert that **at least one** is payable (so the
auto-pick in ClinkServer always has something to choose), while reporting which
ones failed so the shipped default list can be reordered or pruned over time.

Network-dependent and inherently flaky, so it is opt-in only:

    pytest -m live_relay

It self-skips if the machine has no outbound network.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from clink.relay_probe import ProbeStatus, probe_relay_payable

pytestmark = pytest.mark.live_relay

# Per-relay round-trip budget. Generous because public relays can be slow.
PROBE_TIMEOUT = 12.0


def _default_relays() -> list[str]:
    from electrum.simple_config import SimpleConfig
    raw = SimpleConfig.__dict__["NOSTR_RELAYS"].get_default_value()
    return [r.strip() for r in raw.split(",") if r.strip()]


def _has_network() -> bool:
    try:
        socket.create_connection(("relay.damus.io", 443), timeout=5).close()
        return True
    except OSError:
        return False


async def _sweep(relays: list[str]):
    return await asyncio.gather(
        *(probe_relay_payable(r, timeout=PROBE_TIMEOUT) for r in relays)
    )


def test_at_least_one_default_relay_is_payable() -> None:
    if not _has_network():
        pytest.skip("no outbound network")
    relays = _default_relays()
    assert relays, "electrum shipped an empty default NOSTR_RELAYS list"

    results = asyncio.run(_sweep(relays))

    report = "\n".join(
        f"  {r.status.value:<12} {r.relay}"
        + (f"  ({r.rtt_ms} ms)" if r.rtt_ms is not None else "")
        + (f"  [{r.detail}]" if r.detail else "")
        for r in results
    )
    payable = [r for r in results if r.status is ProbeStatus.OK]
    print(f"\nDefault relay payability ({len(payable)}/{len(results)} OK):\n{report}")

    assert payable, (
        "NONE of Electrum's default Nostr relays could carry a CLINK payment "
        f"round-trip, so every default noffer is unpayable:\n{report}"
    )
