## ⚠️ EXPERIMENTAL SOFTWARE

This is experimental software and is released "as-is" without any warranty or guarantees whatsoever. You may lose funds! Consider yourself warned. While we are using this live in production environments, do not attach this software to wallets with significant funds in them.

# 🥂 Electrum CLINK Plugin

This plugin implements the noffer functionality of the 🥂[CLINK protocol](https://clinkme.dev/). This leverages nostr to solve the "I have a lightning wallet but no LNURL or open port to accept payments" problem.

Like an LNURL, an noffer string can be provided to any external wallet to make a payment to your Electrum wallet (provided there is sufficient inbound liquidity). It does not rely on having any ports open, but your wallet must be online to receive the payment (same as any other lightning payment).

⚡ As long as your nostr relay of choice is online, you can receive lightning payments via CLINK!

This software is developed by BareBits. Need simple Bitcoin payments for your point-of-sale store or e-commerce website? We have easy, affordable self-custody solutions and even handle the setup for you. Learn more at [getbarebits.com](https://getbarebits.com)

# 🛠️ Installation Guide

1. Download the zip file from the releases page
2. Go into your Electrum wallet, go to Tools -> Plugins -> Add and add the zip file
3. You can now receive CLINK payments!

## ♥️ Dev Fee

An *optional* .1% dev fee is included by default, which can be disabled in the settings. This dev fee helps fund development and is counted against any funds you receive via CLINK noffers.

## What it does (v1)

* **Generate noffers.** Each offer is a *spontaneous* offer (the payer names the
  amount). The plugin derives a stable Nostr identity from the wallet's
  Lightning node key, so a wallet's noffers survive restarts.
* **Payable-relay auto-pick, pinned per offer.** A noffer embeds exactly one
  relay, and the reference payer connects to *only* that relay — so a dead relay
  yields a noffer that looks fine but is silently unpayable. When you create an
  offer (unless you pin `plugins.clink.relay`), the plugin probes your configured
  relays with a real kind-`21001` write/read-back round-trip, embeds the first
  one that works, and **pins it onto the offer** — the noffer you hand out never
  changes across restarts, and the listener always covers it. The probe result is
  cached for 24h (it only affects which relay *new* offers get); if none pass,
  offer creation is refused. Offers created by older versions are pinned
  automatically at the next successful probe. See `relay_probe.py`.
* **Answer requests.** It subscribes to its relay for kind-`21001` requests,
  NIP-44-decrypts them, and replies with a BOLT-11 invoice — or a structured
  error (NIP-69 codes) when it can't fulfil the request.
* **Offer labels + payer memos.** Each offer carries an editable label, and the
  invoice memo combines that label with the payer's optional NIP-69
  `description` as `"<label> - <description>"`. Folding in the payer memo is a
  per-offer toggle (`allow_payer_memo`, on by default); disable it and invoices
  always carry just the label. Both are editable in the CLINK tab and via CLI.
* **Payment receipts.** When an issued invoice is actually paid, the plugin sends
  the payer a follow-up kind-`21001` event whose decrypted body is `{"res":"ok"}`
  — the receipt the reference `@shocknet/clink-sdk` surfaces via its `onReceipt`
  callback. Owed receipts are persisted, so they survive a relay drop or restart:
  delivery is retried hourly for up to 10 days until the relay accepts it, and
  each accepted receipt is then **re-broadcast several times** over the following
  minutes — see [Receipt re-broadcasts](#receipt-re-broadcasts) below for why
  this deliberate deviation from the reference behaviour exists.
* **Inbound-liquidity locking.** An issued invoice *reserves* the inbound
  liquidity it needs until it is paid or expires (default 300 s, configurable),
  so two concurrent requests can't both be promised the same capacity. A request
  that exceeds available (unreserved) capacity gets `error code 5` with the
  acceptable range. The advertised max is rounded *down* to two significant
  figures so the error can't be used to track the wallet's exact receivable
  balance over time (any amount at or below the advertised max is still
  always accepted).
* **Hostile-input hardening.** Requests are only accepted within a tight
  `created_at` freshness window (an expiration tag can shrink it, never extend
  it), duplicate event ids are dropped, and decrypted payload fields are
  strictly type- and size-validated before use. Each payer pubkey may hold at
  most 200 outstanding unpaid invoices (generous, because one pubkey may be a
  merchant frontend serving many customers); requests beyond that get a
  retryable `error code 2`. Wallet requests behind expired unpaid CLINK
  invoices are garbage-collected automatically, so request spam cannot grow
  the wallet file without bound.
* **Check noffers.** The CLINK tab's "Check noffers" button (and the
  `clink_check_noffers` CLI command) self-tests every offer end to end: the
  plugin plays payer with a throwaway Nostr identity — connect to the noffer's
  embedded relay, send a real encrypted offer request, and verify a valid
  invoice comes back. Results appear in the table's Status column (session-only,
  timestamped). The self-test invoice is unwound immediately: no liquidity is
  held, no wallet request, receipt, or dev-fee entry is left behind. See
  `selftest.py`.
* **Hourly relay liveness check.** A distributed noffer embeds one relay; if
  that relay later dies, the offer silently stops being payable. Every hour (on
  the receipt-retry tick, and once per reconnect) the plugin re-runs the
  payability probe against each relay an existing offer advertises. A failure
  is re-probed once (after 60 s) before being flagged, so a transient blip never
  raises an alarm; a confirmed-down relay is surfaced as a warning banner in the
  CLINK tab (with a ⚠ marker on affected offers) and a log warning — the relay
  selection is never changed behind your back. On demand: `clink_check_relays`.
  See `liveness.py`.
* **Resilient listener lifecycle.** Closing another wallet — or closing and
  reopening the plugin's own wallet (system tray, daemon
  `close_wallet`/`load_wallet`) — restarts the request listener instead of
  silently killing it for the rest of the session. A 60 s watchdog re-attaches
  any pinned relay that was dropped after a transient connect failure, and a
  relay that *rejects* a publish (NIP-20 `OK false`) is reported as an explicit
  "relay refused" failure — with the relay's reason — in the noffer self-test
  and the log, instead of masquerading as a generic "no response". See
  `publish.py` and the lifecycle handling in `clink_plugin.py`.
* **Silent-death detection.** A NAT box, reverse proxy, or dying relay host can
  cull the idle relay websocket without ever sending a close — the connection
  looks healthy while delivering nothing, and the listener used to go deaf
  after long uptimes until the wallet was restarted. Three layered defenses
  (see `nostr_transport.py` and the watchdog in `clink_plugin.py`): every
  listener websocket carries a client-side ping/pong **heartbeat** (30 s; a
  missed pong tears the connection down), the watchdog **prunes** connections
  aiohttp already knows are dead so they get re-attached with a fresh socket
  and re-issued subscription, and every 5 minutes the plugin **self-pings**:
  it publishes a self-addressed (ephemeral, kind-21001) event per relay and
  verifies each echoes back through the live subscription — the only check
  that also catches a half-open connection — reconnecting any relay that stays
  silent. Recovery is per-relay: healthy relays keep serving throughout.
* **Debits / management** (`ndebit` / `nmanage`) are **not** implemented yet;
  they are stubbed via the protocol's "unsupported feature" path so they can be
  added without restructuring.

## Receipt re-broadcasts

This plugin deviates slightly from the CLINK reference implementation, which
publishes a payment receipt exactly once: here, each receipt is re-broadcast a
few extra times over the first minutes after payment (10 s, 15 s, 30 s, 1 m,
2 m and 5 m after the first accepted broadcast).

Why: receipts are kind-`21001` events, which sit in Nostr's *ephemeral* range —
relays deliver them to whoever is subscribed at that instant and are not
expected to store them. A relay accepting the publish therefore proves nothing
about the payer: if the payer's subscription happens to be down at that exact
moment (a checkout page mid-reload, a phone that briefly lost signal, a
payment processor that re-subscribes periodically instead of holding a socket
open), a once-only receipt is lost for good — the invoice is paid, but the
payer can keep showing "waiting for payment" forever. For a merchant that
means support tickets and manual reconciliation for payments that actually
succeeded.

Re-broadcasting closes that window at no cost to compliant payers: every
broadcast is the same NIP-44-encrypted `{"res":"ok"}` receipt for the same
request, so a payer that already saw it treats the duplicates as idempotent
noise, while a payer that missed the first one gets several more chances to
observe the settlement. The schedule is bounded (seven sends, then done) and,
as before, an owed receipt survives restarts: an interrupted schedule resumes
on reconnect. See `RESEND_OFFSETS_SEC` in `receipts.py`.

A *failed* receipt publish (a timeout, a transient network flake, a relay
rejection) is also retried quickly rather than waiting for the hourly retry
tick: for the first ~10 minutes after payment (or after the first accepted
send, once the re-broadcast schedule is running) failures retry on a short
exponential backoff — 5 s, 10 s, 20 s, 40 s, then every 80 s — and a success
resets the backoff. Once that window closes, retries fall back to the hourly
loop, so a relay that is down hard is never hammered for the full 10-day
abandonment bound. See `FAIL_RETRY_BACKOFF_SEC` / `FAST_RETRY_WINDOW_SEC` in
`receipts.py`.

## Layout

```
clink/                 # the importable plugin package (this is what ships)
  __init__.py          # config vars (CLINK_RELAY, CLINK_INVOICE_EXPIRY) + CLI commands
  manifest.json        # plugin metadata (available_for: cmdline, qt)
  clink_plugin.py      # runtime: relay loop + request handler + liquidity lock
  noffer.py            # noffer bech32/TLV codec (byte-identical to @shocknet/clink-sdk)
  relay_probe.py       # payability probe: pick a relay a payer can actually reach
  liveness.py          # hourly re-probe of every relay an existing noffer advertises
  selftest.py          # noffer round-trip self-test ("Check noffers")
  nip44.py             # NIP-44 v2 (validated against the official vectors)
  liquidity.py         # inbound-liquidity reservation
  receipts.py          # persisted payment-receipt registry (retry across restarts)
  offers.py            # offer model + persistence
  protocol.py          # request/response payloads + resolution policy
  cmdline.py, qt.py    # per-GUI bindings (the 'CLINK' tab lives in qt.py)
tests/                 # pytest: unit (offline) + e2e (drives the rig)
scripts/build_zip.py   # package as an Electrum external-plugin zip
```

The plugin depends only on what the host Electrum already bundles
(`electrum_aionostr`, `electrum_ecc`, `electrum.crypto`) — **no extra runtime
dependencies**.

## Configuration

| Config key | Default | Meaning |
|---|---|---|
| `plugins.clink.relay` | `""` (auto-picks a working relay from `NOSTR_RELAYS`) | relay encoded in noffers + subscribed to |
| `plugins.clink.invoice_expiry_sec` | `300` | invoice lifetime **and** liquidity-lock window |
| `plugins.clink.ws_heartbeat_sec` | `30` | listener websocket ping interval; a missed pong drops the connection (0 disables) |
| `plugins.clink.watchdog_interval_sec` | `60` | how often the watchdog checks/re-attaches listener relay connections |
| `plugins.clink.listener_ping_interval_sec` | `300` | how often the listener round-trips a self-addressed event per relay to prove it can still hear (0 disables) |
| `plugins.clink.devfee_enabled` | `true` | collect the optional dev fee (opt-out) |
| `plugins.clink.devfee_rate_percent` | `0.1` | dev-fee rate, % of each inbound payment (0.001–5) |
| `plugins.clink.devfee_dest` | `clink_fees@getbarebits.com` | Lightning address / LNURL / URL the fee is forwarded to |

# Terms of Use

By using this software, you agree not to use it for any purpose which is illegal.

# Privacy

 * This plugin does not collect any information about you or send it anywhere, everything stays local to Electrum.
 * Your chosen nostr relay will have access to some information (your npub, your IP address, etc) to facilitate payment
 * People you give your noffers to will be able to know your relay and other information required to make payments

## CLI commands

When enabled, the plugin registers `clink_`-prefixed commands:

```bash
electrum clink_add_offer --label "coffee"   # -> {offer_id, label, allow_payer_memo, noffer}
electrum clink_add_offer --label "coffee" --allow_payer_memo false  # never fold in payer memos
electrum clink_list_offers
electrum clink_set_offer_label <offer_id> --label "tea"   # rename an offer
electrum clink_set_offer_payer_memo <offer_id> false      # allow/disallow payer memos
electrum clink_remove_offer <offer_id>
electrum clink_check_noffers                # self-test every noffer end to end
electrum clink_check_noffers --offer_id <offer_id>  # ...or just one
electrum clink_check_relays                 # probe every relay your noffers advertise (runs hourly on its own)
electrum clink_clink_status                 # available / reserved liquidity
electrum clink_devfee_status                # dev-fee settings + owed balance
electrum clink_devfee_pay                   # force a payout now (testing)
```

## Tests

```bash
pytest                 # unit tests (offline, fast)
pytest -m e2e          # end-to-end against the regtest rig (slow, needs the rig)
pytest -m live_relay   # probe the real default relays over the network (flaky, opt-in)
```

Relay payability is checked at three levels: `test_relay_probe.py` unit-tests the
probe/selection logic offline; the e2e suite proves a freshly created offer
advertises a relay that round-trips against the rig's own relay; and the opt-in
`live_relay` sweep asserts at least one of Electrum's shipped default relays can
actually carry CLINK traffic, printing a per-relay report so a stale default can
be spotted and pruned.

Unit tests anchor the crypto on authoritative vectors: `noffer` encoding is
byte-checked against `@shocknet/clink-sdk` output, and NIP-44 v2 against the
official `paulmillr/nip44` vectors.

## Development with the regtest rig

The sibling `electrum-regtest-rig` symlinks this package into Electrum's plugins
directory, runs a minimal in-process Nostr relay, enables the plugin and points
it at that relay — so `python run.py` brings up a wallet with a working **CLINK**
tab and seeded Lightning channels for manual testing.

## Packaging

`python scripts/build_zip.py` produces `dist/clink-<version>.zip` laid out as an
Electrum external plugin (top-level `clink/` package + `manifest.json`).

This zip has been verified to load through Electrum's real external-plugin
machinery via the rig's `python run.py --zip-plugin` mode. Note two caveats for
the external path on Electrum 4.7.x:

* **Trust/authorization.** External plugins are gated by `is_authorized()`, which
  verifies an ECDSA signature over the zip hash against a *root-owned* keyfile
  (`/etc/electrum/plugins_key`). For production, an end user authorizes the plugin
  in-app; the rig instead applies a small **env-gated patch** (active only when
  `ELECTRUM_SKIP_PLUGIN_AUTH=1`) to skip it headlessly.
* **Loader bug (multi-module).** Electrum 4.7.x never registers the
  `electrum_external_plugins` namespace package and mis-names the init module, so
  multi-module zip plugins fail to import. The rig's patch fixes this too. CLI
  commands against a zip-mode rig therefore also need `ELECTRUM_SKIP_PLUGIN_AUTH=1`
  in the client's environment.

For day-to-day development the internal symlink install (default rig mode) is
simpler — always authorized, hot-reload, no patch.

## License

Unlicense (public domain) — see [LICENSE](LICENSE).
