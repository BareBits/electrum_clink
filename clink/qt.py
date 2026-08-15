"""Qt GUI for the CLINK plugin: a dedicated 'CLINK' tab.

The tab lets the user create/remove spontaneous offers, view each offer's noffer
(with a QR for scanning), watch live inbound-liquidity reservations, tune the
invoice-expiry / liquidity-lock window, and see a log of recent requests.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional, Tuple

from electrum.gui.common_qt.util import paintQR
from electrum.gui.qt.util import (
    Buttons,
    CancelButton,
    CloseButton,
    ColorScheme,
    OkButton,
    WindowModalDialog,
    read_QIcon,
)
from electrum.i18n import _
from electrum.plugin import hook
from electrum.util import UserCancelled, get_asyncio_loop
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import protocol
from .clink_plugin import ClinkPlugin
from .relay_probe import normalize_relay_url
from .selftest import CheckResult, CheckStatus

# Dev-fee slider bounds (percent). The slider is log-scaled so the low end
# (0.001%–0.1%) is as easy to set as the high end.
DEVFEE_MIN_PCT = 0.001
DEVFEE_MAX_PCT = 5.0
_DEVFEE_SLIDER_STEPS = 1000

# First-run notice (chosen wording — warm & casual).
DEVFEE_NOTICE_TITLE = "A note about the CLINK dev fee 💜"
DEVFEE_NOTICE_TEXT = (
    "👋 Hey there! Quick heads-up: CLINK includes a tiny optional dev fee "
    "(0.1% by default) on payments you receive through your offers. It's our "
    "way of keeping the lights on and shipping new features — basically a "
    "little 'thanks!' to the folks building this. 💜\n\n"
    "Totally optional: tweak it or switch it off anytime in the CLINK tab. "
    "Happy zapping! ⚡"
)


def _devfee_pct_to_slider(pct: float) -> int:
    pct = min(max(pct, DEVFEE_MIN_PCT), DEVFEE_MAX_PCT)
    lo, hi = math.log10(DEVFEE_MIN_PCT), math.log10(DEVFEE_MAX_PCT)
    t = (math.log10(pct) - lo) / (hi - lo)
    return round(t * _DEVFEE_SLIDER_STEPS)


def _devfee_slider_to_pct(value: int) -> float:
    lo, hi = math.log10(DEVFEE_MIN_PCT), math.log10(DEVFEE_MAX_PCT)
    t = value / _DEVFEE_SLIDER_STEPS
    return 10 ** (lo + t * (hi - lo))


def _fmt_pct(pct: float) -> str:
    return f"{pct:.4g}%"


# Offers table columns.
COL_LABEL = 0
COL_MEMO = 1
COL_OFFER = 2
COL_PRICE = 3
COL_NOFFER = 4
COL_RELAY = 5
COL_STATUS = 6

# First entry of the relay dropdown in the "New offer" dialog: use the
# automatically probed relay rather than a fixed one.
RELAY_AUTO_LABEL = _("Automatic (best probed relay)")

# offer_id is stashed on the row (column 0) so label edits never lose the key.
OFFER_ID_ROLE = Qt.ItemDataRole.UserRole
# Base (healthy-state) tooltip of the relay column, stashed on the row so the
# liveness refresh can restore it once a flagged relay recovers.
RELAY_BASE_TOOLTIP_ROLE = Qt.ItemDataRole.UserRole
# Prefix marking a row whose advertised relay failed the last liveness check.
RELAY_DOWN_PREFIX = "⚠ "

# When the CLINK tab is first shown, grow the window *up to* this size so the
# offers table is visible out of the box — never shrinking a larger window.
GROW_TARGET_W = 1000
GROW_TARGET_H = 760

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow
    from electrum.wallet import Abstract_Wallet


class Plugin(ClinkPlugin):
    def __init__(self, *args):
        ClinkPlugin.__init__(self, *args)
        self._tab: Optional["ClinkTab"] = None

    @hook
    def load_wallet(self, wallet: "Abstract_Wallet", window: "ElectrumWindow"):
        if not wallet.has_lightning():
            return
        self.start_plugin(wallet)
        if self._tab is None and self.server is not None:
            self._tab = ClinkTab(self, window)
            window.tabs.addTab(self._tab, read_QIcon("tab_send.png"), _("CLINK"))

    @hook
    def close_wallet(self, *args, **kwargs):
        if self._tab is not None:
            try:
                window = self._tab.window
                idx = window.tabs.indexOf(self._tab)
                if idx >= 0:
                    window.tabs.removeTab(idx)
            except Exception:
                pass
            self._tab = None
        ClinkPlugin.close_wallet(self, *args, **kwargs)


class ClinkTab(QWidget):
    def __init__(self, plugin: ClinkPlugin, window: "ElectrumWindow"):
        QWidget.__init__(self)
        self.plugin = plugin
        self.window = window
        # Guards itemChanged while we repopulate the table, and one-shot window grow.
        self._populating = False
        self._grown = False

        # The whole tab scrolls, so a short Electrum window never hides the
        # offers table or the controls below it (the auto-grow below handles the
        # common case; this is the safety net for small screens / maximized).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)

        root = QVBoxLayout(content)

        header = QLabel("<b>" + _("CLINK Offers") + "</b> — "
                        + _("generate noffers and answer requests with Lightning invoices"))
        root.addWidget(header)

        self.identity_label = QLabel()
        self.identity_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.identity_label)

        # --- liquidity + settings row ------------------------------------
        status_row = QHBoxLayout()
        self.liquidity_label = QLabel()
        status_row.addWidget(self.liquidity_label)
        status_row.addStretch(1)
        status_row.addWidget(QLabel(_("Invoice expiry / liquidity lock (s):")))
        self.expiry_spin = QSpinBox()
        self.expiry_spin.setRange(10, 86400)
        self.expiry_spin.setValue(int(self.plugin.config.CLINK_INVOICE_EXPIRY))
        self.expiry_spin.valueChanged.connect(self._on_expiry_changed)
        status_row.addWidget(self.expiry_spin)
        root.addLayout(status_row)

        # Hidden while every advertised relay passes the hourly liveness check.
        self.relay_health_label = QLabel()
        self.relay_health_label.setWordWrap(True)
        self.relay_health_label.setStyleSheet(ColorScheme.RED.as_stylesheet())
        self.relay_health_label.setVisible(False)
        root.addWidget(self.relay_health_label)

        # --- offers table ------------------------------------------------
        root.addWidget(QLabel(_("Offers (double-click a label to rename):")))
        self.offers_list = QTreeWidget()
        self.offers_list.setHeaderLabels(
            [_("Label"), _("Payer memo"), _("Offer id"), _("Price"),
             _("noffer"), _("Relay"), _("Status")])
        self.offers_list.setRootIsDecorated(False)
        self.offers_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # We drive editing ourselves (only the label column, on double-click), so
        # the default triggers stay off — otherwise other columns could be edited.
        self.offers_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.offers_list.header().setSectionResizeMode(COL_NOFFER, QHeaderView.ResizeMode.Stretch)
        self.offers_list.header().setSectionResizeMode(COL_MEMO, QHeaderView.ResizeMode.ResizeToContents)
        self.offers_list.header().setSectionResizeMode(COL_PRICE, QHeaderView.ResizeMode.ResizeToContents)
        self.offers_list.header().setSectionResizeMode(COL_RELAY, QHeaderView.ResizeMode.ResizeToContents)
        self.offers_list.header().setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        self.offers_list.itemSelectionChanged.connect(self._update_buttons)
        self.offers_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.offers_list.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.offers_list, stretch=2)

        new_btn = QPushButton(_("New offer"))
        new_btn.clicked.connect(self._on_new_offer)
        self.qr_btn = QPushButton(_("Show noffer / QR"))
        self.qr_btn.clicked.connect(self._on_show_qr)
        self.check_btn = QPushButton(_("Check noffers"))
        self.check_btn.setToolTip(_(
            "Verify each noffer end to end: connect to its relay as a payer, "
            "send a test offer request, and confirm a valid invoice comes back."))
        self.check_btn.clicked.connect(self._on_check_noffers)
        self.copy_btn = QPushButton(_("Copy noffer"))
        self.copy_btn.clicked.connect(self._on_copy)
        self.remove_btn = QPushButton(_("Remove"))
        self.remove_btn.clicked.connect(self._on_remove)
        root.addLayout(Buttons(new_btn, self.qr_btn, self.check_btn,
                               self.copy_btn, self.remove_btn))

        # --- recent activity --------------------------------------------
        root.addWidget(QLabel(_("Recent requests:")))
        self.activity = QTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMaximumHeight(140)
        root.addWidget(self.activity, stretch=1)

        # --- dev fee -----------------------------------------------------
        root.addWidget(self._build_devfee_group())

        # --- footer ------------------------------------------------------
        footer = QLabel(
            _("Developed by the team at {}").format(
                '<a href="https://getbarebits.com">BareBits</a>'))
        footer.setOpenExternalLinks(True)
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(footer)

        self._refresh()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_dynamic)
        self._timer.start(2000)

        # Grow the window the first time this tab is shown (see _maybe_grow_window).
        self.window.tabs.currentChanged.connect(self._on_tab_changed)

        self._maybe_show_devfee_notice()

    # -- window sizing ----------------------------------------------------
    def _on_tab_changed(self, index: int) -> None:
        if self.window.tabs.widget(index) is self:
            self._maybe_grow_window()

    def _maybe_grow_window(self) -> None:
        """One-shot: enlarge the window toward GROW_TARGET so the offers table is
        visible. Only ever grows, skips a maximized/fullscreen window, and never
        fights a later manual resize (it runs at most once per tab instance)."""
        if self._grown:
            return
        self._grown = True
        win = self.window
        if win.isMaximized() or win.isFullScreen():
            return
        new_w = max(win.width(), GROW_TARGET_W)
        new_h = max(win.height(), GROW_TARGET_H)
        if new_w != win.width() or new_h != win.height():
            win.resize(new_w, new_h)

    # -- dev fee ----------------------------------------------------------
    def _build_devfee_group(self) -> QGroupBox:
        cfg = self.plugin.config
        box = QGroupBox(_("Dev fee 💜"))
        v = QVBoxLayout(box)
        v.addWidget(QLabel(
            _("An optional contribution that funds further development of CLINK. "
              "It is taken as a small share of payments you receive through your "
              "offers and forwarded once it passes 1,000 sat.")))

        self.devfee_enable = QCheckBox(_("Enable dev fee"))
        self.devfee_enable.setChecked(bool(cfg.CLINK_DEVFEE_ENABLED))
        self.devfee_enable.toggled.connect(self._on_devfee_enabled)
        v.addWidget(self.devfee_enable)

        rate_row = QHBoxLayout()
        rate_row.addWidget(QLabel(_("Rate:")))
        # Show the current percent at the start of the slider (next to "Rate:"),
        # before the slider itself.
        self.devfee_rate_label = QLabel()
        self.devfee_rate_label.setMinimumWidth(70)
        rate_row.addWidget(self.devfee_rate_label)
        self.devfee_slider = QSlider(Qt.Orientation.Horizontal)
        self.devfee_slider.setRange(0, _DEVFEE_SLIDER_STEPS)
        self.devfee_slider.setValue(_devfee_pct_to_slider(float(cfg.CLINK_DEVFEE_RATE_PERCENT)))
        self.devfee_slider.valueChanged.connect(self._on_devfee_rate_changed)
        rate_row.addWidget(self.devfee_slider, stretch=1)
        v.addLayout(rate_row)

        self.devfee_owed_label = QLabel()
        v.addWidget(self.devfee_owed_label)

        self._sync_devfee_rate_label()
        self._sync_devfee_enabled_state()
        return box

    def _on_devfee_enabled(self, checked: bool) -> None:
        self.plugin.config.CLINK_DEVFEE_ENABLED = bool(checked)
        self._sync_devfee_enabled_state()

    def _sync_devfee_enabled_state(self) -> None:
        self.devfee_slider.setEnabled(self.devfee_enable.isChecked())

    def _on_devfee_rate_changed(self, value: int) -> None:
        pct = _devfee_slider_to_pct(value)
        self.plugin.config.CLINK_DEVFEE_RATE_PERCENT = float(pct)
        self._sync_devfee_rate_label()

    def _sync_devfee_rate_label(self) -> None:
        pct = _devfee_slider_to_pct(self.devfee_slider.value())
        self.devfee_rate_label.setText(_fmt_pct(pct))

    def _maybe_show_devfee_notice(self) -> None:
        cfg = self.plugin.config
        if cfg.CLINK_DEVFEE_NOTICE_SHOWN:
            return
        cfg.CLINK_DEVFEE_NOTICE_SHOWN = True
        QMessageBox.information(self, _(DEVFEE_NOTICE_TITLE), _(DEVFEE_NOTICE_TEXT))

    # -- helpers ----------------------------------------------------------
    def _selected_noffer(self) -> Optional[str]:
        items = self.offers_list.selectedItems()
        return items[0].text(COL_NOFFER) if items else None

    def _selected_offer_id(self) -> Optional[str]:
        items = self.offers_list.selectedItems()
        return items[0].data(COL_LABEL, OFFER_ID_ROLE) if items else None

    def _update_buttons(self) -> None:
        has_sel = bool(self.offers_list.selectedItems())
        for btn in (self.qr_btn, self.copy_btn, self.remove_btn):
            btn.setEnabled(has_sel)

    def _on_expiry_changed(self, value: int) -> None:
        self.plugin.config.CLINK_INVOICE_EXPIRY = int(value)

    def _on_new_offer(self) -> None:
        result = self._prompt_offer_details()
        if result is None:
            return
        label, allow_memo, price, relay = result
        # create_offer probes the relay (the auto pick is cached 24h; a failing
        # probe blocks creation for custom and automatic relays alike), so run
        # it in a waiting dialog rather than freezing the tab.
        message = (_("Checking that the chosen relay can carry this offer…")
                   if relay else
                   _("Checking that a Nostr relay can carry this offer…"))
        try:
            self.window.run_coroutine_dialog(
                self.plugin.create_offer(
                    label=label, allow_payer_memo=allow_memo, relay=relay,
                    price=price),
                message,
            )
        except UserCancelled:
            return
        except Exception as e:
            self.window.show_error(_("Could not create offer: {}").format(e))
            return
        self._refresh()

    def _prompt_offer_details(self) -> Optional[Tuple[str, bool, Optional[int], str]]:
        """Ask for a new offer's label, memo policy, optional fixed price and relay.

        Returns ``(label, allow_payer_memo, price, relay)`` — ``price`` is
        ``None`` for a spontaneous offer, ``relay`` is ``""`` for automatic
        selection — or ``None`` if cancelled. A malformed typed relay or a
        non-integer price re-opens the dialog (with the inputs preserved)
        instead of accepting.
        """
        d = WindowModalDialog(self.window, _("New CLINK offer"))
        vbox = QVBoxLayout(d)
        vbox.addWidget(QLabel(_("Label (optional):")))
        label_edit = QLineEdit()
        label_edit.setPlaceholderText(_("e.g. Coffee stand"))
        vbox.addWidget(label_edit)
        memo_cb = QCheckBox(_("Allow payer-selected memos"))
        memo_cb.setChecked(True)
        memo_cb.setToolTip(_(
            "When enabled, a note sent by the payer is folded into the invoice "
            "memo. When disabled, the invoice always uses this offer's label."))
        vbox.addWidget(memo_cb)
        vbox.addWidget(QLabel(_("Fixed price in sats (optional):")))
        price_edit = QLineEdit()
        price_edit.setPlaceholderText(_("e.g. 25000 — leave empty for a "
                                        "spontaneous offer"))
        price_edit.setToolTip(_(
            "A positive amount in sats. When set, this becomes a fixed-price "
            "offer: its noffer advertises the price and every invoice is minted "
            "for exactly this amount. Leave empty for a spontaneous offer whose "
            "amount the payer chooses."))
        vbox.addWidget(price_edit)
        vbox.addWidget(QLabel(_("Relay:")))
        relay_combo = QComboBox()
        relay_combo.setEditable(True)
        relay_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        relay_combo.addItem(RELAY_AUTO_LABEL)
        for url in self.plugin.candidate_relay_urls():
            relay_combo.addItem(url)
        relay_combo.setToolTip(_(
            "The Nostr relay this offer's noffer advertises, pinned to the "
            "offer at creation. Keep \"{}\" to let the plugin pick a working "
            "relay, choose one from the list, or type your own in the form "
            "wss://myrelay.com:port. The relay is tested first; the offer is "
            "only created if the test passes."
            ).format(RELAY_AUTO_LABEL))
        vbox.addWidget(relay_combo)
        vbox.addLayout(Buttons(CancelButton(d), OkButton(d)))
        label_edit.setFocus()
        while True:
            if not d.exec():
                return None
            price_text = price_edit.text().strip()
            if price_text:
                if not (price_text.isascii() and price_text.isdigit()):
                    self.window.show_error(_("Invalid price: must be a positive "
                                             "integer amount in sats."))
                    continue
                # "0" is treated as "no fixed price" (spontaneous), matching the
                # create_offer API.
                price = int(price_text) or None
            else:
                price = None
            relay_text = relay_combo.currentText().strip()
            if not relay_text or relay_text == RELAY_AUTO_LABEL:
                relay = ""
            else:
                try:
                    relay = normalize_relay_url(relay_text)
                except ValueError as e:
                    self.window.show_error(_("Invalid relay URL: {}").format(e))
                    continue
            return label_edit.text().strip(), memo_cb.isChecked(), price, relay

    def _on_remove(self) -> None:
        offer_id = self._selected_offer_id()
        if offer_id is None:
            return
        self.plugin.remove_offer(offer_id)
        self._refresh()

    # -- inline edits (label rename + payer-memo toggle) ------------------
    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        # Only the label column is user-editable; the rest are read-only.
        if column == COL_LABEL:
            self.offers_list.editItem(item, COL_LABEL)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._populating:
            return
        offer_id = item.data(COL_LABEL, OFFER_ID_ROLE)
        if not offer_id:
            return
        if column == COL_LABEL:
            self.plugin.set_offer_label(offer_id, item.text(COL_LABEL).strip())
        elif column == COL_MEMO:
            allow = item.checkState(COL_MEMO) == Qt.CheckState.Checked
            self.plugin.set_offer_allow_payer_memo(offer_id, allow)

    def _on_copy(self) -> None:
        noffer = self._selected_noffer()
        if noffer:
            QApplication.clipboard().setText(noffer)
            self.window.show_message(_("noffer copied to clipboard"))

    # -- noffer self-test --------------------------------------------------
    def _on_check_noffers(self) -> None:
        if not self.plugin.list_offers():
            self.window.show_message(_("No offers to check yet."))
            return
        if self.plugin.noffer_check_running():
            return  # button is disabled while a run is live; belt and braces
        # Fire the check on the asyncio loop; the 2s poller renders "Checking…"
        # rows and the results as they land, and re-enables the button. Errors
        # are logged rather than raised into Qt (per-offer failures are already
        # folded into each offer's result).
        future = asyncio.run_coroutine_threadsafe(
            self.plugin.check_noffers(), get_asyncio_loop())
        future.add_done_callback(self._on_check_done)
        self._refresh_dynamic()

    def _on_check_done(self, future) -> None:
        # Runs on the asyncio thread — no Qt calls here; the poller repaints.
        try:
            future.result()
        except Exception as e:
            self.plugin.logger.info(f"noffer check failed: {e!r}")

    def _status_cell(self, result: Optional[CheckResult]) -> Tuple[str, str]:
        """Render one offer's self-test result as (cell text, tooltip)."""
        if self.plugin.noffer_check_running():
            return _("Checking…"), _("A noffer check is in progress.")
        if result is None:
            return "—", _("Not checked yet. Press \"Check noffers\".")
        when = datetime.fromtimestamp(result.checked_at).strftime("%H:%M")
        rtt = f" in {result.rtt_ms/1000:.1f}s" if result.rtt_ms is not None else ""
        if result.status is CheckStatus.OK:
            return (_("✓ OK ({})").format(when),
                    _("Round trip succeeded{}: the relay carried the request and "
                      "a valid invoice came back.").format(rtt))
        if result.status is CheckStatus.LISTENER_ERROR:
            # The round trip itself worked — the service answered with a CLINK
            # error. Surface the two commonest causes in plain words.
            if result.error_code == protocol.ERR_INVALID_AMOUNT:
                return (_("⚠ No inbound liquidity ({})").format(when),
                        _("The noffer round trip works, but the wallet cannot "
                          "receive even 1 sat right now, so payers get an "
                          "error. Open a channel or free up inbound capacity."))
            if result.error_code == protocol.ERR_INVALID_OFFER:
                return (_("✗ Unknown offer ({})").format(when),
                        _("The service answered, but does not know this offer."))
            return (_("⚠ Service error ({})").format(when),
                    _("The service replied with an error: {} (code {})").format(
                        result.detail or "?", result.error_code))
        text = {
            CheckStatus.UNREACHABLE: _("✗ Relay unreachable ({})"),
            CheckStatus.PUBLISH_FAILED: _("✗ Relay refused request ({})"),
            CheckStatus.NO_RESPONSE: _("✗ No response ({})"),
            CheckStatus.BAD_RESPONSE: _("✗ Invalid response ({})"),
            CheckStatus.INVALID_NOFFER: _("✗ Invalid noffer ({})"),
        }.get(result.status, _("✗ Check failed ({})")).format(when)
        tooltip = {
            CheckStatus.UNREACHABLE: _(
                "Could not connect to the relay this noffer advertises. "
                "Payers cannot reach you through it."),
            CheckStatus.PUBLISH_FAILED: _(
                "Connected, but the relay refused the test payment request."),
            CheckStatus.NO_RESPONSE: _(
                "The relay accepted the request but no reply arrived — the "
                "plugin's listener may not be connected to this relay."),
            CheckStatus.BAD_RESPONSE: _("A reply arrived but was not valid."),
            CheckStatus.INVALID_NOFFER: _("This noffer string does not decode."),
        }.get(result.status, result.detail or _("Unexpected failure while checking."))
        if result.detail and result.status not in (CheckStatus.INVALID_NOFFER,):
            tooltip += f"\n{result.detail}"
        return text, tooltip

    def _retired_state(self, item: Any) -> Optional[Tuple[str, str]]:
        """(text, tooltip) when this offer is moved or expired, else ``None``.

        A replaced offer stopped answering and points its payers at a new
        noffer; a time-expired offer answers code 3 with no pointer. Either
        state is surfaced in the status column ahead of any self-test result,
        since the check would only confirm the expiry.
        """
        info = self.plugin.list_offers().get(item.data(COL_LABEL, OFFER_ID_ROLE))
        if not info:
            return None
        if info.get("replaced_by"):
            return (_("→ moved"), _(
                "This offer is retired and redirects payers to its replacement "
                "noffer automatically (code 3, 'latest')."))
        expires_at = info.get("expires_at") or 0
        if expires_at and expires_at <= int(time.time()):
            return (_("✗ expired"), _(
                "This offer passed its expiry time; payers get a code-3 "
                "'expired' response and no invoice."))
        return None

    def _refresh_check_column(self) -> None:
        results = self.plugin.noffer_check_results()
        self.check_btn.setText(
            _("Checking…") if self.plugin.noffer_check_running() else _("Check noffers"))
        self.check_btn.setEnabled(not self.plugin.noffer_check_running())
        for i in range(self.offers_list.topLevelItemCount()):
            item = self.offers_list.topLevelItem(i)
            offer_id = item.data(COL_LABEL, OFFER_ID_ROLE)
            retired = self._retired_state(item)
            if retired is not None:
                text, tooltip = retired
            else:
                text, tooltip = self._status_cell(results.get(offer_id))
            if item.text(COL_STATUS) != text:
                item.setText(COL_STATUS, text)
            item.setToolTip(COL_STATUS, tooltip)

    def _refresh_relay_health(self) -> None:
        """Surface relays that failed the hourly liveness check (retried once).

        A banner lists every down relay; affected offer rows get a ⚠ marker on
        the relay column. Both clear on their own once a relay recovers.
        """
        liveness = self.plugin.relay_liveness()
        down = {url: res for url, res in liveness.items() if not res.get("ok")}
        if down:
            parts = []
            for url, res in down.items():
                when = datetime.fromtimestamp(res["checked_at"]).strftime("%H:%M")
                parts.append(f"{url} ({res['status']}, {when})")
            self.relay_health_label.setText(RELAY_DOWN_PREFIX + _(
                "Relay failing the periodic liveness check (retried once): {} — "
                "offers advertising it may currently be unpayable. Consider "
                "moving affected offers to a working relay."
                ).format("; ".join(parts)))
        self.relay_health_label.setVisible(bool(down))

        for i in range(self.offers_list.topLevelItemCount()):
            item = self.offers_list.topLevelItem(i)
            relay = item.text(COL_RELAY).removeprefix(RELAY_DOWN_PREFIX)
            base_tooltip = item.data(COL_RELAY, RELAY_BASE_TOOLTIP_ROLE) or ""
            if relay in down:
                text = RELAY_DOWN_PREFIX + relay
                tooltip = base_tooltip + "\n" + _(
                    "This relay failed the last liveness check ({}); payers "
                    "may not reach this offer through it."
                    ).format(down[relay]["status"])
            else:
                text, tooltip = relay, base_tooltip
            if item.text(COL_RELAY) != text:
                item.setText(COL_RELAY, text)
            item.setToolTip(COL_RELAY, tooltip)

    def _on_show_qr(self) -> None:
        noffer = self._selected_noffer()
        if not noffer:
            return
        d = WindowModalDialog(self.window, _("noffer"))
        vbox = QVBoxLayout(d)
        qr: Optional[QImage] = paintQR(noffer)
        if qr:
            label = QLabel()
            label.setPixmap(QPixmap.fromImage(qr))
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            vbox.addWidget(label)
        text = QTextEdit()
        text.setText(noffer)
        text.setReadOnly(True)
        text.setMaximumHeight(80)
        vbox.addWidget(text)
        vbox.addLayout(Buttons(CloseButton(d)))
        d.exec()

    # -- refresh ----------------------------------------------------------
    def _refresh(self) -> None:
        self.identity_label.setText(
            _("Identity pubkey:") + f" {self.plugin.identity_pubkey or '—'}")
        selected = self._selected_noffer()
        # Suppress itemChanged while we rebuild rows (setText/setCheckState fire it).
        self._populating = True
        try:
            self.offers_list.clear()
            for offer_id, info in self.plugin.list_offers().items():
                price_type = info.get("price_type")
                price = info.get("price")
                if price_type == 0 and isinstance(price, int) and price > 0:
                    price_text = _("fixed {} sat").format(price)
                    price_tooltip = _(
                        "Fixed-price offer: every invoice is minted for exactly "
                        "{} sats. The payer cannot name a different amount.")
                    price_tooltip = price_tooltip.format(price)
                elif price_type == 1:
                    price_text = _("variable")
                    price_tooltip = _("Variable-price offer.")
                else:
                    price_text = _("spontaneous")
                    price_tooltip = _(
                        "Spontaneous offer: the payer chooses the amount.")
                item = QTreeWidgetItem(
                    [info["label"], "", offer_id, price_text, info["noffer"],
                     info.get("relay", ""), ""])
                expires_at = info.get("expires_at") or 0
                if expires_at:
                    when = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M")
                    price_tooltip += "\n" + _(
                        "This offer expires at {}; payers get a code-3 'expired' "
                        "response afterwards.").format(when)
                if info.get("replaced_by"):
                    price_tooltip += "\n" + _(
                        "This offer is retired and redirects payers to its "
                        "replacement noffer (code 3, 'latest').")
                item.setToolTip(COL_PRICE, price_tooltip)
                item.setData(COL_LABEL, OFFER_ID_ROLE, offer_id)
                if info.get("relay_custom"):
                    relay_tooltip = _(
                        "Relay this noffer advertises — chosen for this offer.")
                elif info.get("relay_pinned"):
                    relay_tooltip = _(
                        "Relay this noffer advertises — picked automatically "
                        "when the offer was created and pinned to it, so the "
                        "noffer never changes.")
                else:
                    relay_tooltip = _(
                        "Relay this noffer advertises — not yet pinned (offer "
                        "predates relay pinning); it is pinned automatically "
                        "at the next successful relay probe.")
                item.setToolTip(COL_RELAY, relay_tooltip)
                item.setData(COL_RELAY, RELAY_BASE_TOOLTIP_ROLE, relay_tooltip)
                item.setFlags(item.flags()
                              | Qt.ItemFlag.ItemIsEditable
                              | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    COL_MEMO,
                    Qt.CheckState.Checked if info.get("allow_payer_memo", True)
                    else Qt.CheckState.Unchecked)
                item.setToolTip(COL_MEMO, _(
                    "Allow a payer-supplied memo to be folded into the invoice. "
                    "Click to toggle."))
                self.offers_list.addTopLevelItem(item)
                if info["noffer"] == selected:
                    item.setSelected(True)
        finally:
            self._populating = False
        self._update_buttons()
        self._refresh_dynamic()

    def _refresh_dynamic(self) -> None:
        # Driven by a 2s QTimer, so it can fire while the wallet is being torn
        # down. The plugin getters already degrade gracefully, but guard here
        # too so a teardown race never crashes the timer or spams tracebacks.
        try:
            self._do_refresh_dynamic()
        except Exception as e:
            self.plugin.logger.info(f"clink tab refresh skipped: {e!r}")

    def _do_refresh_dynamic(self) -> None:
        status = self.plugin.liquidity_status()
        self.liquidity_label.setText(_("Inbound liquidity — available: {} sat | locked: {} sat ({} active)").format(
            status["available_sat"], status["reserved_sat"], status["active_reservations"]))
        lines = []
        for entry in reversed(self.plugin.recent_activity()):
            ts = datetime.fromtimestamp(entry["time"]).strftime("%H:%M:%S")
            amt = entry["amount_sat"]
            amt_str = f"{amt} sat" if amt is not None else "—"
            lines.append(f"{ts}  offer={entry['offer'] or '?'}  {amt_str}  →  {entry['result']}")
        self.activity.setPlainText("\n".join(lines))

        devfee = self.plugin.devfee_status()
        self.devfee_owed_label.setText(_("Owed: {} sat — forwarded to {}").format(
            devfee["owed_sat"], devfee["destination"] or "—"))

        self._refresh_check_column()
        self._refresh_relay_health()
