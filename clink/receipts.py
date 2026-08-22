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

  remember()    -> awaiting payment (``due=False``); dropped by sweep() if the
                   invoice expires unpaid.
  mark_due()    -> the invoice was paid; a receipt is now owed (``due=True``).
                   Persisted *before* the send is attempted, so a crash or relay
                   failure mid-send still leaves the receipt owed.
  record_send() -> a broadcast of the receipt reached the relay. Because the
                   receipt is an ephemeral (kind-21001) event that relays don't
                   store, one accepted broadcast only proves the *relay* saw it —
                   not the payer. So the entry stays owed and the receipt is
                   re-broadcast at each :data:`RESEND_OFFSETS_SEC` offset after
                   the first accepted send; the final send removes the entry.

A still-owed (``due``) receipt that has never been successfully broadcast is
retried at most once per :data:`RETRY_INTERVAL_SEC`; one mid re-broadcast
schedule follows :data:`RESEND_OFFSETS_SEC` instead. A *failed* publish inside
the first :data:`FAST_RETRY_WINDOW_SEC` is additionally fast-retried on the
:data:`FAIL_RETRY_BACKOFF_SEC` backoff (see :meth:`ReceiptRegistry.fail_retry_delay`),
so a transient network flake never parks a receipt for a whole retry interval.
Either way an owed entry is finally abandoned :data:`RETRY_MAX_SEC` after
payment, so the map can never grow without bound.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, MutableMapping, Optional

# Retry an owed-but-undelivered receipt at most this often.
RETRY_INTERVAL_SEC: int = 60 * 60          # hourly
# Give up on an owed receipt this long after the invoice was paid.
RETRY_MAX_SEC: int = 10 * 24 * 60 * 60     # 10 days
# After the first *successful* broadcast, re-broadcast the receipt once per
# entry here (seconds after that first send) — 7 sends in total over 5
# minutes. Receipts are ephemeral events, so a payer whose subscription was
# down at the instant of a broadcast can only be reached by another one; see
# the README's "Receipt re-broadcasts" section.
RESEND_OFFSETS_SEC: "tuple[int, ...]" = (10, 15, 30, 60, 120, 300)
# In-session backoff after a *failed* publish of an owed receipt, while the
# entry is inside the fast-retry window below. Consecutive failures walk this
# table (staying on the last entry); a successful publish resets the streak.
FAIL_RETRY_BACKOFF_SEC: "tuple[int, ...]" = (5, 10, 20, 40, 80)
# Failed publishes are fast-retried only this long after the entry's schedule
# anchor (payment time for a never-sent receipt, first accepted send once the
# re-broadcast schedule is running). Afterwards the hourly retry loop takes
# over, so a relay that is down hard is never hammered for the full 10-day
# abandonment bound.
FAST_RETRY_WINDOW_SEC: int = RESEND_OFFSETS_SEC[-1] + 300  # 10 minutes
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
    # Successful broadcasts so far (0 = never reached the relay yet).
    sends: int = 0


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
            sends=int(entry.get("sends", 0)),
        )

    # --- lifecycle -------------------------------------------------------

    def remember(self, rhash: str, payer_pubkey: str, request_event_id: str,
                 expires_at: float) -> List[str]:
        """Record that an invoice was issued; a receipt may later be owed.

        ``expires_at`` is the invoice's own expiry: if the invoice is never paid,
        :meth:`sweep` drops the entry once this passes (no receipt is ever owed).

        Returns the payment hashes of any awaiting-payment entries evicted to
        stay under :data:`MAX_PENDING`, so the caller can garbage-collect their
        wallet requests too — an evicted entry must not leave an orphaned
        request behind.
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
        evicted = self._enforce_cap(entries)
        self._save(entries)
        return evicted

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

    def record_send(self, rhash: str) -> Optional[float]:
        """A broadcast of this receipt reached the relay (``OK true``).

        Returns the delay in seconds until the next scheduled re-broadcast
        (the next :data:`RESEND_OFFSETS_SEC` offset, measured from the *first*
        accepted send — so a late re-broadcast doesn't push the rest of the
        schedule out), or ``None`` when the schedule is complete — the final
        send removes the entry, so nothing more is owed. Unknown ``rhash``
        also returns ``None``.

        One accepted broadcast proves only that the *relay* saw the receipt;
        kind-21001 events are ephemeral (not stored), so a payer who wasn't
        subscribed at that instant needs a later broadcast to ever learn the
        invoice was paid. Hence the entry stays owed through
        :data:`RESEND_OFFSETS_SEC`.
        """
        entries = self._load()
        entry = entries.get(rhash)
        if entry is None:
            return None
        now = float(self._now_fn())
        sends = int(entry.get("sends", 0)) + 1
        if sends > len(RESEND_OFFSETS_SEC):
            entries.pop(rhash, None)
            self._save(entries)
            return None
        entry["sends"] = sends
        entry["last_send"] = now
        if sends == 1:
            entry["first_send"] = now
        self._save(entries)
        first = float(entry.get("first_send", now))
        return max(0.0, first + RESEND_OFFSETS_SEC[sends - 1] - now)

    def is_owed(self, rhash: str) -> bool:
        """Whether a receipt is still owed (paid, schedule not yet complete)."""
        entry = self._load().get(rhash)
        return bool(entry and entry.get("due"))

    def fail_retry_delay(self, rhash: str, fail_streak: int) -> Optional[float]:
        """Backoff before the next in-session retry of a *failed* publish.

        ``fail_streak`` is the number of consecutive failed publishes so far,
        including the one just observed (so the first failure passes ``1`` and
        gets the shortest backoff). Returns ``None`` when the entry is not owed
        or its :data:`FAST_RETRY_WINDOW_SEC` has closed — from then on the
        hourly redelivery loop is the retry path, exactly as before fast
        retries existed. The window is anchored to the first accepted send once
        the re-broadcast schedule is running (so a schedule that *started* late
        still gets fast retries), and to the payment time before that.
        """
        entry = self._load().get(rhash)
        if not entry or not entry.get("due"):
            return None
        now = float(self._now_fn())
        if int(entry.get("sends", 0)) > 0:
            anchor = float(entry.get("first_send", 0.0))
        else:
            anchor = float(entry.get("due_since", 0.0))
        if now - anchor >= FAST_RETRY_WINDOW_SEC:
            return None
        idx = min(max(fail_streak, 1), len(FAIL_RETRY_BACKOFF_SEC)) - 1
        return float(FAIL_RETRY_BACKOFF_SEC[idx])

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
        """Owed receipts whose retry/re-broadcast interval has elapsed.

        Used by the runtime's periodic redelivery loop and on reconnect/startup.
        A never-broadcast entry that has never been attempted
        (``last_attempt == 0``) is always returned; otherwise it must be at
        least :data:`RETRY_INTERVAL_SEC` old. An entry mid re-broadcast schedule
        (``sends > 0``) follows :data:`RESEND_OFFSETS_SEC` instead — the
        in-session cadence is driven by the runtime's scheduled tail, so this
        path exists to *resume* an interrupted schedule after a restart or
        reconnect.
        """
        entries, _expired = self._sweep(self._load())
        now = self._now_fn()
        out: List[ReceiptTarget] = []
        for rhash, entry in entries.items():
            if not entry.get("due"):
                continue
            sends = int(entry.get("sends", 0))
            if sends > 0:
                first = float(entry.get("first_send", entry.get("last_send", 0.0)))
                offset = RESEND_OFFSETS_SEC[min(sends, len(RESEND_OFFSETS_SEC)) - 1]
                if now - first < offset:
                    continue
                out.append(self._target(rhash, entry))
                continue
            last = float(entry.get("last_attempt", 0.0))
            if last and now - last < RETRY_INTERVAL_SEC:
                continue
            out.append(self._target(rhash, entry))
        return out

    # --- maintenance -----------------------------------------------------

    def sweep(self) -> List[str]:
        """Drop expired-unpaid and abandoned (over-retried) entries; persist.

        Returns the payment hashes of the *expired-unpaid* entries dropped, so
        the runtime can garbage-collect their wallet requests. Abandoned owed
        entries are never returned: their invoices were paid, and a paid request
        is the merchant's record.
        """
        before = self._load()
        after, expired_unpaid = self._sweep(dict(before))
        if after != before:
            self._save(after)
        return expired_unpaid

    def _sweep(self, entries: Dict[str, Dict[str, Any]],
               ) -> "tuple[Dict[str, Dict[str, Any]], List[str]]":
        now = self._now_fn()
        kept: Dict[str, Dict[str, Any]] = {}
        expired_unpaid: List[str] = []
        for rhash, entry in entries.items():
            if entry.get("due"):
                # Owed: keep retrying until we give up RETRY_MAX_SEC after payment.
                if now - float(entry.get("due_since", now)) >= RETRY_MAX_SEC:
                    continue
            else:
                # Not yet paid: once the invoice expires, no receipt is ever owed.
                if now >= float(entry.get("expires_at", 0.0)):
                    expired_unpaid.append(rhash)
                    continue
            kept[rhash] = entry
        return kept, expired_unpaid

    def _enforce_cap(self, entries: Dict[str, Dict[str, Any]]) -> List[str]:
        """Bound the map: evict the soonest-to-expire *un-owed* entries first.

        Returns the evicted payment hashes (empty when under the cap).
        """
        if len(entries) <= MAX_PENDING:
            return []
        # Never evict an owed receipt to make room; only prune awaiting-payment
        # ones (the oldest by expiry), which would be swept shortly anyway.
        prunable = [k for k, e in entries.items() if not e.get("due")]
        prunable.sort(key=lambda k: float(entries[k].get("expires_at", 0.0)))
        evicted = prunable[: len(entries) - MAX_PENDING]
        for k in evicted:
            entries.pop(k, None)
        return evicted

    # --- introspection ---------------------------------------------------

    def pending_count(self) -> int:
        return len(self._load())

    def pending_count_for(self, payer_pubkey: str) -> int:
        """Awaiting-payment (not yet paid, unexpired) entries for one payer.

        Backs the per-payer cap on outstanding unpaid invoices. Expired entries
        are excluded even before a sweep prunes them, so a stale registry can
        never lock a payer out longer than their invoices actually lived.
        """
        now = self._now_fn()
        return sum(
            1 for e in self._load().values()
            if not e.get("due")
            and e.get("payer") == payer_pubkey
            and now < float(e.get("expires_at", 0.0))
        )

    def owed_count(self) -> int:
        return sum(1 for e in self._load().values() if e.get("due"))
