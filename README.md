## ⚠️ EXPERIMENTAL SOFTWARE

This is experimental software and is released "as-is" without any warranty or guarantees whatsoever. You may lose funds! Consider yourself warned. While we are using this live in production environments, do not attach this software to wallets with significant funds in them.

# 🥂 Electrum CLINK Plugin

This plugin implements the noffer functionality of the 🥂[CLINK protocol](https://clinkme.dev/). This leverages nostr to solve the "I have a lightning wallet but no LNURL or open port to accept payments" option.

Like an LNURL, an noffer string can be provided to any external wallet to make a payment to your Electrum wallet (provided there is sufficient inbound liquidity). It does not rely on having any ports open, but your wallet must be online to receive the payment (same as any other lightning payment).

⚡ As long as your nost relay of choice is online, you can receive lightning payments via CLINK!

This software is developed by BareBits. Need simple Bitcoin payments for your point-of-sale store or e-commerce website? We have easy, affordable self-custody solutions and even handle the setup for you. Learn more at [getbarebits.com](https://getbarebits.com)

## ♥️ Dev Fee

An *optional* .1% dev fee is included by default, which can be disabled in the settings. This dev fee helps fund development and is counted against any funds you receive 

# 🛠️ Installation Guide

1. Download the zip file from the releases page
2. Go into your Electrum wallet, go to Tools -> Plugins -> Add and add the zip file
3. You can now receive CLINK payments!

# License

CLINK is released into the public domain. 

# Terms of Use

By using this software, you agree not to use it for any purpose which is illegal.

# Privacy

 * This plugin does not collect any information about you or send it anywhere, everything stays local to Electrum.
 * Your chosen nostr relay will have access to some information (your npub, your IP address, etc) to facilitate payment
 * People you give your noffers to will be able to know your relay and other information required to make payments
