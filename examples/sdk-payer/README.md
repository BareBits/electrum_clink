# External CLINK payer (official SDK)

Request a Lightning invoice from a `noffer` using the reference
[`@shocknet/clink-sdk`](https://www.npmjs.com/package/@shocknet/clink-sdk) — an
*independent* implementation, so a successful run proves real interop with the
Electrum CLINK plugin, not just our own code round-tripping.

## Setup

```bash
cd examples/sdk-payer
npm install
```

## Use

1. Generate an offer in the running Electrum wallet (rig example):

   ```bash
   electrum --regtest --dir <datadir> clink_add_offer --label "test"
   # -> { "noffer": "noffer1...", ... }
   ```

2. Request an invoice for it (run on the **same machine** as the rig, since the
   noffer's relay is `ws://127.0.0.1:<port>`):

   ```bash
   node pay.mjs noffer1... 1500       # request 1500 sat
   ```

   On success it prints a `lnbcrt…` BOLT-11 invoice; otherwise it prints the
   CLINK error payload (e.g. `{"code":5,"error":"Invalid Amount","range":{...}}`
   when the amount exceeds available inbound liquidity).

## How it works

`decodeBech32(noffer)` yields the receiver pubkey, relay and offer id;
`SendNofferRequest` then sends a NIP-44-encrypted kind-21001 request over that
relay and waits for the kind-21001 reply — exactly the flow the plugin answers.
