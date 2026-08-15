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

## What it does

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
  the payer a follow-up kind-`21001` event whose decrypted body is
  `{"res":"ok","preimage":"<settlement preimage>"}` — the receipt the reference
  `@shocknet/clink-sdk` surfaces via its `onReceipt` callback (and the exact
  `res`/`preimage` shape the `NofferReceipt` type expects). Owed receipts are
  persisted, so they survive a relay drop or restart: delivery is retried hourly
  for up to 10 days until the relay accepts it.
* **Payer-requested invoice expiry.** A request may name how long its invoice
  should stay valid via `expires_in_seconds`; the value is honored but clamped
  to a 60 s–24 h sanity window, so a hostile payer can never pin inbound
  liquidity for longer than a day nor mint an unpayably short invoice. The same
  value drives the bolt11 `exp_delay`, the liquidity reservation, and the
  receipt registry's expiry.
* **Fixed-price offers.** An offer can pin a fixed price (`clink_add_offer
  --price`); requests that don't match it are refused with error code 5 naming
  the exact price, instead of a range. Spontaneous offers (no price) keep the
  range semantics.
* **Inbound-liquidity locking.** An issued invoice *reserves* the inbound
  liquidity it needs until it is paid or expires (default 120 s, configurable),
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
* **Debits / management** (`ndebit` / `nmanage`) are **not** implemented yet;
  they are stubbed via the protocol's "unsupported feature" path so they can be
  added without restructuring.
* **Offer expiry & moves.** An offer can be given an absolute lifetime
  (`clink_add_offer --expires-in`); once past it, the offer stops answering and
  requests reply with error code 3 ("expired"). `clink_replace_offer` moves
  payers from one offer onto another: the outgoing offer replies with error
  code 3 ("moved") carrying the replacement's noffer in the `latest` field, so a
  payer holding a stale noffer updates and retries automatically. Both code-3
  payloads carry the `latest` noffer that should be used going forward.
* **Kind-0 metadata advertisement.** The default offer's noffer is advertised in
  the identity's Nostr kind-0 profile metadata (`clink_offer` field), so a
  `kind0 -> clink_offer`-style lookup resolves straight to a payable noffer. The
  plugin fetches the identity's current metadata, merges the noffer in
  (preserving every other profile field), and republishes only when something
  changed. Opt out via `plugins.clink.advertise_metadata`; reconcile on demand
  with `clink_advertise_offer` and preview with `clink_metadata_status`.
* **NIP-57 zaps.** A request may carry a signed kind-`9734` zap request in its
  `zap` field. It is validated per NIP-57 (exactly one `p` tag naming us, at
  most one each of `e`/`a`, well-formed `k`/`P`, ≥1 `relays` tag, fresh
  `created_at`, and a real BIP-340 signature — forged events are refused), and
  the zap amount (msat, rounded up) is authoritative for the invoice. When the
  zap is settled, the plugin publishes a kind-`9735` zap receipt (with
  `p`/`P`/`e`/`a`/`k`/`bolt11`/`description`/`preimage` tags) to the relay the
  payer named. See `zap.py`.

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
  metadata.py          # kind-0 metadata advertisement (merge/select helpers)
  zap.py               # NIP-57 zap requests + kind-9735 receipts (pure logic)
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
| `plugins.clink.invoice_expiry_sec` | `120` | invoice lifetime **and** liquidity-lock window |
| `plugins.clink.devfee_enabled` | `true` | collect the optional dev fee (opt-out) |
| `plugins.clink.devfee_rate_percent` | `0.1` | dev-fee rate, % of each inbound payment (0.001–5) |
| `plugins.clink.devfee_dest` | `clink_fees@getbarebits.com` | Lightning address / LNURL / URL the fee is forwarded to |
| `plugins.clink.advertise_metadata` | `true` | publish the default offer's noffer in the identity's kind-0 profile metadata (`clink_offer` field) |

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
electrum clink_add_offer --price 5000                  # fixed-price offer (5000 sats)
electrum clink_add_offer --expires-in 3600             # offer expires in 1h (code-3 "expired" after)
electrum clink_list_offers
electrum clink_set_offer_label <offer_id> --label "tea"   # rename an offer
electrum clink_set_offer_payer_memo <offer_id> false      # allow/disallow payer memos
electrum clink_replace_offer <offer_id> <replacement_id>  # move payers, code-3 "moved" with the new noffer
electrum clink_remove_offer <offer_id>
electrum clink_check_noffers                # self-test every noffer end to end
electrum clink_check_noffers --offer_id <offer_id>  # ...or just one
electrum clink_check_relays                 # probe every relay your noffers advertise (runs hourly on its own)
electrum clink_clink_status                 # available / reserved liquidity
electrum clink_devfee_status                # dev-fee settings + owed balance
electrum clink_devfee_pay                   # force a payout now (testing)
electrum clink_advertise_offer              # reconcile the kind-0 metadata advertisement now
electrum clink_metadata_status              # preview what the kind-0 advertisement would publish
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
