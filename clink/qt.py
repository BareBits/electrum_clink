"""Qt GUI for the CLINK plugin: a dedicated 'CLINK' tab.

The tab lets the user create/remove spontaneous offers, view each offer's noffer
(with a QR for scanning), watch live inbound-liquidity reservations, tune the
invoice-expiry / liquidity-lock window, and see a log of recent requests.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPixmap, QImage
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPushButton, QSlider, QSpinBox, QTextEdit,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from electrum.i18n import _
from electrum.plugin import hook
from electrum.gui.qt.util import Buttons, CloseButton, WindowModalDialog, read_QIcon
from electrum.gui.common_qt.util import paintQR

from .clink_plugin import ClinkPlugin

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

if TYPE_CHECKING:
    from electrum.wallet import Abstract_Wallet
    from electrum.gui.qt.main_window import ElectrumWindow


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

        root = QVBoxLayout(self)

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

        # --- offers table ------------------------------------------------
        root.addWidget(QLabel(_("Offers:")))
        self.offers_list = QTreeWidget()
        self.offers_list.setHeaderLabels([_("Label"), _("Offer id"), _("noffer")])
        self.offers_list.setRootIsDecorated(False)
        self.offers_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.offers_list.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.offers_list.itemSelectionChanged.connect(self._update_buttons)
        root.addWidget(self.offers_list, stretch=2)

        new_btn = QPushButton(_("New offer"))
        new_btn.clicked.connect(self._on_new_offer)
        self.qr_btn = QPushButton(_("Show noffer / QR"))
        self.qr_btn.clicked.connect(self._on_show_qr)
        self.copy_btn = QPushButton(_("Copy noffer"))
        self.copy_btn.clicked.connect(self._on_copy)
        self.remove_btn = QPushButton(_("Remove"))
        self.remove_btn.clicked.connect(self._on_remove)
        root.addLayout(Buttons(new_btn, self.qr_btn, self.copy_btn, self.remove_btn))

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

        self._maybe_show_devfee_notice()

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
        self.devfee_slider = QSlider(Qt.Orientation.Horizontal)
        self.devfee_slider.setRange(0, _DEVFEE_SLIDER_STEPS)
        self.devfee_slider.setValue(_devfee_pct_to_slider(float(cfg.CLINK_DEVFEE_RATE_PERCENT)))
        self.devfee_slider.valueChanged.connect(self._on_devfee_rate_changed)
        rate_row.addWidget(self.devfee_slider, stretch=1)
        self.devfee_rate_label = QLabel()
        self.devfee_rate_label.setMinimumWidth(70)
        rate_row.addWidget(self.devfee_rate_label)
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
        return items[0].text(2) if items else None

    def _update_buttons(self) -> None:
        has_sel = bool(self.offers_list.selectedItems())
        for btn in (self.qr_btn, self.copy_btn, self.remove_btn):
            btn.setEnabled(has_sel)

    def _on_expiry_changed(self, value: int) -> None:
        self.plugin.config.CLINK_INVOICE_EXPIRY = int(value)

    def _on_new_offer(self) -> None:
        self.plugin.create_offer(label="")
        self._refresh()

    def _on_remove(self) -> None:
        items = self.offers_list.selectedItems()
        if not items:
            return
        self.plugin.remove_offer(items[0].text(1))
        self._refresh()

    def _on_copy(self) -> None:
        noffer = self._selected_noffer()
        if noffer:
            QApplication.clipboard().setText(noffer)
            self.window.show_message(_("noffer copied to clipboard"))

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
        self.offers_list.clear()
        for offer_id, info in self.plugin.list_offers().items():
            item = QTreeWidgetItem([info["label"] or _("(no label)"), offer_id, info["noffer"]])
            self.offers_list.addTopLevelItem(item)
            if info["noffer"] == selected:
                item.setSelected(True)
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
