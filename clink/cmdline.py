"""Command-line GUI binding for the CLINK plugin."""

from typing import TYPE_CHECKING

from electrum.plugin import hook

from .clink_plugin import ClinkPlugin

if TYPE_CHECKING:
    from electrum.daemon import Daemon
    from electrum.wallet import Abstract_Wallet


class Plugin(ClinkPlugin):

    def __init__(self, *args):
        ClinkPlugin.__init__(self, *args)

    @hook
    def daemon_wallet_loaded(self, daemon: "Daemon", wallet: "Abstract_Wallet"):
        self.start_plugin(wallet)
