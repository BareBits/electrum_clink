# Electrum CLINK plugin

An Electrum plugin that speaks the **CLINK** offers protocol: it generates
`noffer` static payment codes and answers payment requests over Nostr by
returning fresh BOLT-11 Lightning invoices.

CLINK ("Common Lightning Interface for Nostr Keys") runs entirely over Nostr —
no HTTP callbacks, no SSL/domain requirements. See the
[spec](https://clinkme.dev/specs.html) and [NIP-69](https://github.com/nostr-protocol/nips/pull/1460).

## What it does (v1)

* **Generate noffers.** Each offer is a *spontaneous* offer (the payer names the
  amount). The plugin derives a stable Nostr identity from the wallet's
  Lightning node key, so a wallet's noffers survive restarts.
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
  delivery is retried hourly for up to 10 days until the relay accepts it.
* **Inbound-liquidity locking.** An issued invoice *reserves* the inbound
  liquidity it needs until it is paid or expires (default 120 s, configurable),
  so two concurrent requests can't both be promised the same capacity. A request
  that exceeds available (unreserved) capacity gets `error code 5` with the
  acceptable range.
* **Debits / management** (`ndebit` / `nmanage`) are **not** implemented yet;
  they are stubbed via the protocol's "unsupported feature" path so they can be
  added without restructuring.

## Layout

```
clink/                 # the importable plugin package (this is what ships)
  __init__.py          # config vars (CLINK_RELAY, CLINK_INVOICE_EXPIRY) + CLI commands
  manifest.json        # plugin metadata (available_for: cmdline, qt)
  clink_plugin.py      # runtime: relay loop + request handler + liquidity lock
  noffer.py            # noffer bech32/TLV codec (byte-identical to @shocknet/clink-sdk)
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
| `plugins.clink.relay` | `""` (falls back to `NOSTR_RELAYS[0]`) | relay encoded in noffers + subscribed to |
| `plugins.clink.invoice_expiry_sec` | `120` | invoice lifetime **and** liquidity-lock window |
| `plugins.clink.devfee_enabled` | `true` | collect the optional dev fee (opt-out) |
| `plugins.clink.devfee_rate_percent` | `0.1` | dev-fee rate, % of each inbound payment (0.001–5) |
| `plugins.clink.devfee_dest` | `clink_fees@getbarebits.com` | Lightning address / LNURL / URL the fee is forwarded to |

## Dev fee

An optional, opt-out contribution that funds further development. It accrues as a
small share (default 0.1%) of every inbound payment answered through a CLINK
offer, tracked in millisats so small payments aren't lost to rounding. Once the
owed balance passes **1,000 sat** the plugin forwards it to `devfee_dest` over
LNURL-pay. Safeguards: at most **one payout attempt per 24h** (success or
failure), and never more than **10,000 sat within any 24h window**. The rate is
adjustable (or the fee disabled entirely) from a slider in the CLINK tab, which
also shows a one-time first-run notice.

## CLI commands

When enabled, the plugin registers `clink_`-prefixed commands:

```bash
electrum clink_add_offer --label "coffee"   # -> {offer_id, label, allow_payer_memo, noffer}
electrum clink_add_offer --label "coffee" --allow_payer_memo false  # never fold in payer memos
electrum clink_list_offers
electrum clink_set_offer_label <offer_id> --label "tea"   # rename an offer
electrum clink_set_offer_payer_memo <offer_id> false      # allow/disallow payer memos
electrum clink_remove_offer <offer_id>
electrum clink_clink_status                 # available / reserved liquidity
electrum clink_devfee_status                # dev-fee settings + owed balance
electrum clink_devfee_pay                   # force a payout now (testing)
```

## Tests

```bash
pytest                 # unit tests (offline, fast)
pytest -m e2e          # end-to-end against the regtest rig (slow, needs the rig)
```

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

MIT — see [LICENSE](LICENSE).
