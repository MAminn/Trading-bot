"""Durable outbox for execution-critical engine signals.

The engine computes a signal, the app's signal API records it, and the separate
multi-tenant executor acts on it. The transport between the first two used to be
fire-and-forget with no retries, which meant a failed POST silently discarded the
message. For a CLOSE signal that is unacceptable: the executor places only MARKET
orders, so a position's only exit is the engine's close signal reaching it.

This module is the durability layer. It is deliberately standalone — it imports
nothing from the engine, opens no network connection of its own, and knows
nothing about Binance, credentials, sizing or orders. The bridge supplies a
`poster` callable; everything else here is bookkeeping.

TWO-PHASE PROTOCOL
------------------
The hazard is delivering a signal the engine has not durably committed: the
sender POSTs, the executor moves a real position, then the engine's own persist
fails and its state rolls back. Reality has moved and the strategy has not.

So a row is written PREPARED and only becomes READY once the engine's state is
durable. The sender delivers READY only.

    prepare()  ->  PREPARED
                      |  engine state durable (save_runtime_state returned)
                      v
                    READY  ->  HTTP 2xx  ->  ACKED
                      ^
                      |  startup recovery, when the bar was already durable
                   PREPARED

    HELD  is a terminal hold for operator attention: payload divergence on
          recovery, or an orphan whose bar left the engine's fetch window.
          Never auto-promoted, never delivered, NEVER deleted.

CRASH WINDOWS
-------------
A. Crash after PREPARED, before the engine state is durable.
   The row is KEPT PREPARED. It is *not* deleted: on restart the engine may
   reclassify that bar as CATCHUP (which the bridge suppresses), so relying on
   reprocessing to regenerate it would lose the signal permanently. The bridge
   recognises the surviving PREPARED row and completes the interrupted
   transaction instead.

B. Crash after the engine state is durable, before promote().
   The bar will never be reprocessed, so this row is the only record. Startup
   recovery promotes it.

The two are told apart by comparing the row's bar against the engine's own
persisted `last_processed_bar` — two durable values, no wall clock, no timing
assumption.

DELIVERY POLICY (signal lane)
-----------------------------
Retry indefinitely until the backend acknowledges. Exponential backoff with a
cap. No automatic DEAD state: we never knowingly discard an execution-critical
signal and keep trading. 4xx is retried too — a persistent 400 is a loud,
standing fault for an operator, not licence to drop a CLOSE.

Strict FIFO per lane. The sender takes the lowest-id row that is not ACKED and
delivers only if it is READY; anything else makes it WAIT. A CLOSE must never be
overtaken by a later OPEN, so head-of-line blocking here is the correct
behaviour, not a defect.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("outbox")

# Row states.
PREPARED = "PREPARED"
READY = "READY"
ACKED = "ACKED"
HELD = "HELD"

# Lanes. Only the signal lane exists in phase 1; trade reporting stays outside
# the outbox until the trade endpoint is idempotent by trade_id.
KIND_SIGNAL = "signal"

# Backoff: 1, 2, 4, 8 ... capped. One signal per 15m, so the cap dominates only
# during a genuine outage.
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 300.0

# Consecutive failures before the lane is reported degraded. The engine emits a
# bar every 15 minutes; five failures is minutes of trying, not seconds.
ESCALATE_AFTER_ATTEMPTS = 5

# Verdicts returned by deliver_once(), so the caller (and tests) can drive the
# sender one step at a time without threads.
IDLE = "IDLE"
BLOCKED = "BLOCKED"
WAITING = "WAITING"
DELIVERED = "DELIVERED"
RETRY = "RETRY"

# (ok, status_code, error)
PostResult = Tuple[bool, Optional[int], Optional[str]]
Poster = Callable[[str, Dict[str, Any]], PostResult]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  kind            TEXT    NOT NULL,
  event_key       TEXT    NOT NULL,
  bar_epoch_ms    INTEGER NOT NULL,
  path            TEXT    NOT NULL,
  payload         TEXT    NOT NULL,
  status          TEXT    NOT NULL,
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL    NOT NULL DEFAULT 0,
  last_error      TEXT,
  held_reason     TEXT,
  created_at      REAL    NOT NULL,
  updated_at      REAL    NOT NULL,
  UNIQUE(kind, event_key)
);
CREATE INDEX IF NOT EXISTS outbox_lane_idx ON outbox(kind, id);
"""


def canonical_json(payload: Dict[str, Any]) -> str:
    """Stable serialisation used both for storage and for equality on recovery.

    Sorted keys and fixed separators, so the same payload always produces the
    same bytes. Every field the bridge puts in a payload is deterministic in
    (row, state); the non-deterministic values in the engine's event (RUN_ID,
    logged_at_utc) are excluded by the bridge's allowlist and never reach here.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class OutboxRow:
    id: int
    kind: str
    event_key: str
    bar_epoch_ms: int
    path: str
    payload: str
    status: str
    attempts: int
    next_attempt_at: float
    last_error: Optional[str]
    held_reason: Optional[str]

    def payload_dict(self) -> Dict[str, Any]:
        return json.loads(self.payload)


def _row(r: sqlite3.Row) -> OutboxRow:
    return OutboxRow(
        id=r["id"],
        kind=r["kind"],
        event_key=r["event_key"],
        bar_epoch_ms=r["bar_epoch_ms"],
        path=r["path"],
        payload=r["payload"],
        status=r["status"],
        attempts=r["attempts"],
        next_attempt_at=r["next_attempt_at"],
        last_error=r["last_error"],
        held_reason=r["held_reason"],
    )


class Outbox:
    """SQLite-backed durable queue. One instance per process.

    `poster` is injected so this module never imports requests and tests never
    touch the network. `clock` is injected so backoff is testable without sleeps.
    """

    def __init__(
        self,
        db_path: str,
        poster: Optional[Poster] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db_path = str(db_path)
        self._poster = poster
        self._clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # check_same_thread=False plus an RLock: the bar thread writes and the
        # sender thread reads/updates. Volume is one row per 15 minutes, so a
        # single guarded connection is simpler and safer than a pool.
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL for reader/writer concurrency; FULL because this fsync is the
        # entire point of the module. One small write per bar makes the cost
        # irrelevant.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        self.stop_sender()
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # write path (called on the engine's bar thread)
    # ------------------------------------------------------------------ #

    def prepare(
        self,
        event_key: str,
        bar_epoch_ms: int,
        path: str,
        payload: Dict[str, Any],
        kind: str = KIND_SIGNAL,
    ) -> None:
        """Durably record a signal as PREPARED. Commits before returning.

        A retried bar refreshes its own PREPARED payload; a row that has already
        reached READY, ACKED or HELD is never mutated, so a redelivery in flight
        can never have the message changed underneath it.
        """
        now = self._clock()
        blob = canonical_json(payload)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO outbox
                  (kind, event_key, bar_epoch_ms, path, payload, status,
                   attempts, next_attempt_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                ON CONFLICT(kind, event_key) DO UPDATE SET
                  payload    = excluded.payload,
                  updated_at = excluded.updated_at
                WHERE outbox.status = ?
                """,
                (kind, event_key, int(bar_epoch_ms), path, blob, PREPARED, now, now, PREPARED),
            )
            self._conn.commit()

    def promote(self, event_key: str, kind: str = KIND_SIGNAL) -> bool:
        """PREPARED -> READY. Called only after the engine's state is durable."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE outbox SET status=?, updated_at=? "
                "WHERE kind=? AND event_key=? AND status=?",
                (READY, self._clock(), kind, event_key, PREPARED),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def hold(self, event_key: str, reason: str, kind: str = KIND_SIGNAL) -> bool:
        """Move a row to HELD. Terminal until an operator acts.

        Never deletes. A held row sits at the head of its lane and blocks every
        later signal, which is the point: if an execution-critical signal cannot
        be delivered, later ones must not be delivered around it.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE outbox SET status=?, held_reason=?, updated_at=? "
                "WHERE kind=? AND event_key=? AND status IN (?, ?)",
                (HELD, reason, self._clock(), kind, event_key, PREPARED, READY),
            )
            self._conn.commit()
        if cur.rowcount:
            log.error("[outbox] HELD | key=%s | %s", event_key, reason)
        return cur.rowcount > 0

    def get(self, event_key: str, kind: str = KIND_SIGNAL) -> Optional[OutboxRow]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM outbox WHERE kind=? AND event_key=?", (kind, event_key)
            ).fetchone()
        return _row(r) if r else None

    # ------------------------------------------------------------------ #
    # recovery
    # ------------------------------------------------------------------ #

    def recover(
        self, last_processed_bar_ms: Optional[int], kind: str = KIND_SIGNAL
    ) -> Dict[str, int]:
        """Resolve PREPARED rows left behind by a crash. Deletes nothing.

        bar <= last_processed_bar : the engine's state for that bar was already
                                    durable, so the bar will never be
                                    reprocessed and this row is the only record
                                    -> promote to READY.
        bar >  last_processed_bar : the engine never durably advanced past the
                                    bar -> KEEP PREPARED. The bridge completes
                                    the interrupted transaction when the bar is
                                    reprocessed, whatever mode it is then
                                    classified as.

        Must run before the sender starts.
        """
        promoted = 0
        kept = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outbox WHERE kind=? AND status=? ORDER BY id",
                (kind, PREPARED),
            ).fetchall()
            for r in rows:
                if last_processed_bar_ms is not None and r["bar_epoch_ms"] <= int(
                    last_processed_bar_ms
                ):
                    self._conn.execute(
                        "UPDATE outbox SET status=?, updated_at=? WHERE id=?",
                        (READY, self._clock(), r["id"]),
                    )
                    promoted += 1
                    log.warning(
                        "[outbox] recovery: promoted orphan PREPARED -> READY | key=%s",
                        r["event_key"],
                    )
                else:
                    kept += 1
                    log.warning(
                        "[outbox] recovery: kept interrupted LIVE bar PREPARED | key=%s",
                        r["event_key"],
                    )
            self._conn.commit()
        return {"promoted": promoted, "kept": kept}

    def detect_skipped_orphans(
        self,
        durable_bar_epoch_ms: int,
        exclude_event_key: Optional[str] = None,
        kind: str = KIND_SIGNAL,
    ) -> List[str]:
        """Hold PREPARED rows the engine has advanced past without recovering.

        The engine fetches a bounded window of closed bars (OUTPUTSIZE), so an
        outage longer than that window leaves a PREPARED bar that can never
        appear in `new_rows` again. Observing the engine durably process a LATER
        bar while an earlier PREPARED row is still pending proves it will never
        be reprocessed.

        Called after every successful persist — including ordinary CATCHUP, so a
        stale orphan is still detected on a run that prepares nothing.
        """
        held: List[str] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outbox WHERE kind=? AND status=? AND bar_epoch_ms < ? "
                "ORDER BY id",
                (kind, PREPARED, int(durable_bar_epoch_ms)),
            ).fetchall()
            for r in rows:
                if exclude_event_key is not None and r["event_key"] == exclude_event_key:
                    continue
                self._conn.execute(
                    "UPDATE outbox SET status=?, held_reason=?, updated_at=? WHERE id=?",
                    (
                        HELD,
                        "bar left the engine fetch window; unrecoverable",
                        self._clock(),
                        r["id"],
                    ),
                )
                held.append(r["event_key"])
            self._conn.commit()
        for key in held:
            log.error(
                "[outbox] ORPHAN UNRECOVERABLE | key=%s | engine durable past it "
                "without reprocessing; lane blocked for operator",
                key,
            )
        return held

    # ------------------------------------------------------------------ #
    # delivery
    # ------------------------------------------------------------------ #

    def head(self, kind: str = KIND_SIGNAL) -> Optional[OutboxRow]:
        """Lowest-id row that is not ACKED — the lane's blocking position."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM outbox WHERE kind=? AND status!=? ORDER BY id LIMIT 1",
                (kind, ACKED),
            ).fetchone()
        return _row(r) if r else None

    def deliver_once(self, kind: str = KIND_SIGNAL) -> str:
        """Attempt one delivery step. Returns a verdict constant.

        Strict FIFO: only the head is ever considered. A head that is not READY
        blocks the lane rather than being skipped.
        """
        row = self.head(kind)
        if row is None:
            return IDLE
        if row.status != READY:
            # PREPARED (engine state not yet durable) or HELD (operator hold).
            return BLOCKED
        now = self._clock()
        if row.next_attempt_at > now:
            return WAITING
        if self._poster is None:
            return WAITING

        try:
            ok, status_code, error = self._poster(row.path, row.payload_dict())
        except Exception as exc:  # noqa: BLE001 - transport must never escape
            ok, status_code, error = False, None, repr(exc)

        if ok:
            with self._lock:
                self._conn.execute(
                    "UPDATE outbox SET status=?, last_error=NULL, updated_at=? WHERE id=?",
                    (ACKED, now, row.id),
                )
                self._conn.commit()
            if row.attempts:
                log.warning(
                    "[outbox] delivered after %d failed attempt(s) | key=%s",
                    row.attempts,
                    row.event_key,
                )
            return DELIVERED

        attempts = row.attempts + 1
        delay = min(BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)), BACKOFF_CAP_SECONDS)
        detail = f"HTTP {status_code}" if status_code is not None else (error or "unknown")
        with self._lock:
            self._conn.execute(
                "UPDATE outbox SET attempts=?, next_attempt_at=?, last_error=?, "
                "updated_at=? WHERE id=?",
                (attempts, now + delay, detail[:500], now, row.id),
            )
            self._conn.commit()
        # No DEAD state, including for 4xx: a standing 4xx is a loud fault for an
        # operator, never licence to drop an execution-critical signal.
        logfn = log.error if attempts >= ESCALATE_AFTER_ATTEMPTS else log.warning
        logfn(
            "[outbox] delivery failed | key=%s | attempt=%d | %s | retry in %.0fs",
            row.event_key,
            attempts,
            detail,
            delay,
        )
        return RETRY

    # ------------------------------------------------------------------ #
    # degraded reporting
    # ------------------------------------------------------------------ #

    def degraded(self, kind: str = KIND_SIGNAL) -> Optional[Dict[str, Any]]:
        """Describe why the lane is stuck, or None when healthy.

        The bridge's heartbeat reads this and reports status="error", which the
        existing heartbeat schema already accepts — no API change.
        """
        row = self.head(kind)
        if row is None:
            return None
        if row.status == HELD:
            return {
                "reason": "held",
                "event_key": row.event_key,
                "detail": row.held_reason,
                "attempts": row.attempts,
            }
        if row.status == READY and row.attempts >= ESCALATE_AFTER_ATTEMPTS:
            return {
                "reason": "undelivered",
                "event_key": row.event_key,
                "detail": row.last_error,
                "attempts": row.attempts,
            }
        return None

    def stats(self, kind: str = KIND_SIGNAL) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM outbox WHERE kind=? GROUP BY status",
                (kind,),
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ------------------------------------------------------------------ #
    # background sender
    # ------------------------------------------------------------------ #

    def start_sender(self, poll_seconds: float = 1.0) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.is_set():
                try:
                    verdict = self.deliver_once()
                except Exception as exc:  # noqa: BLE001 - the sender must not die
                    log.error("[outbox] sender error: %s", exc, exc_info=True)
                    verdict = RETRY
                if verdict == DELIVERED:
                    continue  # drain the lane without pausing
                self._stop.wait(poll_seconds)

        self._thread = threading.Thread(target=_loop, name="outbox-sender", daemon=True)
        self._thread.start()
        log.info("[outbox] sender started (db=%s)", self._db_path)

    def stop_sender(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
