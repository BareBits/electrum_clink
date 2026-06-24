// CLINK external payer using the official @shocknet/clink-sdk.
//
// Requests a Lightning invoice from a `noffer` over Nostr — an independent
// client (the reference SDK) exercising the Electrum plugin's service side.
//
//   node pay.mjs <noffer> [amount_sats]
//
// The noffer embeds the relay + receiver pubkey + offer id, so nothing else is
// needed. For the rig the relay is ws://127.0.0.1:<port>, so run this on the
// same machine.

import { SimplePool, useWebSocketImplementation } from 'nostr-tools/pool';
import { generateSecretKey } from 'nostr-tools/pure';
import WebSocket from 'ws';
useWebSocketImplementation(WebSocket); // Node < 22 has no global WebSocket

import { SendNofferRequest, decodeBech32 } from '@shocknet/clink-sdk';

const noffer = process.argv[2];
const amount = parseInt(process.argv[3] || '1000', 10);
if (!noffer) {
  console.error('usage: node pay.mjs <noffer> [amount_sats]');
  process.exit(2);
}

const { data } = decodeBech32(noffer); // { pubkey, relay, offer, priceType, price }
console.error(`decoded noffer -> relay=${data.relay} offer=${data.offer} priceType=${data.priceType}`);

const pool = new SimplePool();
const sk = generateSecretKey();
const resp = await SendNofferRequest(
  pool, sk, [data.relay], data.pubkey,
  { offer: data.offer, amount_sats: amount },
  30, // timeout seconds
);

if (resp && resp.bolt11) {
  console.log('\nINVOICE:', resp.bolt11);
} else {
  console.log('\nERROR RESPONSE:', JSON.stringify(resp));
}
process.exit(0);
