"""Persisted registry of payment receipts owed to payers.

The CLINK offers flow has two halves. First we answer a kind-21001 *request* with
an invoice (handled elsewhere). Second, once that invoice is actually paid, we
owe the payer a *receipt*: a follow-up kind-21001 event whose decrypted body is
``{"res": "ok"}`` (the reference ``@shocknet/clink-sdk`` delivers it to the
payer's ``onReceipt`` callback). This module remembers, for every invoice we
issue, who to send that receipt to and which request it answers — so a receipt
can still be delivered after a relay drop, reconnect, or full Electrum restart.

Design mirrors :mod:`clink.devfee` / :mod:`clink.offers`: pure
accounting/bookkeeping over an injected ``storage`` mapping (the wallet DB's
plugin storage) plus an injected clock, so it is fully unit-testable with a plain
dict. All relay/crypto I/O lives in the runtime.

Lifecycle of one entry, keyed by the invoice payment hash (``rhash``):

  remember()  -> awaiting payment (``due=False``); dropped by sweep() if the
                 invoice expires unpaid.
  mark_due()  -> the invoice was paid; a receipt is now owed (``due=True``).
                 Persisted *before* the send is attempted, so a crash or relay
                 failure mid-send still leaves the receipt owed.
  mark_sent() -> the receipt reached the relay; entry removed.

A still-owed (``due``) receipt is retried at most once per
:data:`RETRY_INTERVAL_SEC` and finally abandoned after
:data:`RETRY_MAX_SEC`, so the map can never grow without bound.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, MutableMapping, Optional

# Retry an owed-but-undelivered receipt at most this often.
RETRY_INTERVAL_SEC: int = 60 * 60          # hourly
# Give up on an owed receipt this long after the invoice was paid.
RETRY_MAX_SEC: int = 10 * 24 * 60 * 60     # 10 days
# Hard cap on remembered entries, so unpaid/never-swept invoices can't grow the
# map without limit; oldest-by-expiry are evicted first.
MAX_PENDING: int = 1_000


@dataclass
class ReceiptTarget:
    """Everything needed to address a receipt back to the original payer."""
    rhash: str
    payer_pubkey: str
    request_event_id: str
    attempts: int = 0
    due_since: float = 0.0


class ReceiptRegistry:
    """Persisted map of ``rhash -> pending receipt``, keyed by payment hash.

    ``storage`` is any mutable mapping the host persists (the plugin passes the
    wallet DB's plugin storage); ``clock_fn`` returns the current unix time.
    """

    STORAGE_KEY = "receipts_pending"

    def __init__(
        self,
        storage: MutableMapping[str, Any],
        *,
        clock_fn: Callable[[], float] = time.time,
    ) -> None:
        self._storage = storage
        self._now_fn = clock_fn

    # --- persistence helpers --------------------------------------------

    def _load(self) -> Dict[str, Dict[str, Any]]:
        raw = self._storage.get(self.STORAGE_KEY)
        return dict(raw) if isinstance(raw, dict) else {}

    def _save(self, entries: Dict[str, Dict[str, Any]]) -> None:
        self._storage[self.STORAGE_KEY] = entries

    @staticmethod
    def _target(rhash: str, entry: Dict[str, Any]) -> ReceiptTarget:
        return ReceiptTarget(
            rhash=rhash,
            payer_pubkey=str(entry.get("payer", "")),
            request_event_id=str(entry.get("req", "")),
            attempts=int(entry.get("attempts", 0)),
            due_since=float(entry.get("due_since", 0.0)),
        )

    # --- lifecycle -------------------------------------------------------

    def remember(self, rhash: str, payer_pubkey: str, request_event_id: str,
                 expires_at: float) -> None:
        """Record that an invoice was issued; a receipt may later be owed.

        ``expires_at`` is the invoice's own expiry: if the invoice is never paid,
        :meth:`sweep` drops the entry once this passes (no receipt is ever owed).
        """
        entries = self._load()
        entries[rhash] = {
            "payer": payer_pubkey,
            "req": request_event_id,
            "expires_at": float(expires_at),
            "due": False,
            "due_since": 0.0,
            "attempts": 0,
            "last_attempt": 0.0,
        }
        self._enforce_cap(entries)
        self._save(entries)

    def forget(self, rhash: str) -> None:
        """Drop a remembered invoice (e.g. it was cancelled before payment)."""
        entries = self._load()
        if entries.pop(rhash, None) is not None:
            self._save(entries)

    def mark_due(self, rhash: str) -> Optional[ReceiptTarget]:
        """The invoice was paid: a receipt is now owed. Returns its target.

        Returns ``None`` when ``rhash`` is not one of ours (e.g. a payment to an
        invoice we did not issue for an offer). Persists the owed state *before*
        any send is attempted, so the receipt survives a failed/dropped delivery.
        """
        entries = self._load()
        entry = entries.get(rhash)
        if entry is None:
            return None
        if not entry.get("due"):
            entry["due"] = True
            entry["due_since"] = float(self._now_fn())
        self._save(entries)
        return self._target(rhash, entry)

    def mark_sent(self, rhash: str) -> None:
        """The receipt reached the relay: stop owing it."""
        entries = self._load()
        if entries.pop(rhash, None) is not None:
            self._save(entries)

    def record_attempt(self, rhash: str) -> None:
        """Stamp a delivery attempt so the next retry waits a full interval."""
        entries = self._load()
        entry = entries.get(rhash)
        if entry is None:
            return
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry["last_attempt"] = float(self._now_fn())
        self._save(entries)

    # --- retry queue -----------------------------------------------------

    def due_targets(self) -> List[ReceiptTarget]:
        """Owed receipts whose retry interval has elapsed, after pruning.

        Used by the runtime's periodic redelivery loop and on reconnect/startup.
        An entry that has never been attempted (``last_attempt == 0``) is always
        returned; otherwise it must be at least :data:`RETRY_INTERVAL_SEC` old.
        """
        entries = self._sweep(self._load())
        now = self._now_fn()
        out: List[ReceiptTarget] = []
        for rhash, entry in entries.items():
            if not entry.get("due"):
                continue
            last = float(entry.get("last_attempt", 0.0))
            if last and now - last < RETRY_INTERVAL_SEC:
                continue
            out.append(self._target(rhash, entry))
        return out

    # --- maintenance -----------------------------------------------------

    def sweep(self) -> None:
        """Drop expired-unpaid and abandoned (over-retried) entries; persist."""
        before = self._load()
        after = self._sweep(dict(before))
        if after != before:
            self._save(after)

    def _sweep(self, entries: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        now = self._now_fn()
        kept: Dict[str, Dict[str, Any]] = {}
        for rhash, entry in entries.items():
            if entry.get("due"):
                # Owed: keep retrying until we give up RETRY_MAX_SEC after payment.
                if now - float(entry.get("due_since", now)) >= RETRY_MAX_SEC:
                    continue
            else:
                # Not yet paid: once the invoice expires, no receipt is ever owed.
                if now >= float(entry.get("expires_at", 0.0)):
                    continue
            kept[rhash] = entry
        return kept

    def _enforce_cap(self, entries: Dict[str, Dict[str, Any]]) -> None:
        """Bound the map: evict the soonest-to-expire *un-owed* entries first."""
        if len(entries) <= MAX_PENDING:
            return
        # Never evict an owed receipt to make room; only prune awaiting-payment
        # ones (the oldest by expiry), which would be swept shortly anyway.
        prunable = [k for k, e in entries.items() if not e.get("due")]
        prunable.sort(key=lambda k: float(entries[k].get("expires_at", 0.0)))
        for k in prunable[: len(entries) - MAX_PENDING]:
            entries.pop(k, None)

    # --- introspection ---------------------------------------------------

    def pending_count(self) -> int:
        return len(self._load())

    def owed_count(self) -> int:
        return sum(1 for e in self._load().values() if e.get("due"))
