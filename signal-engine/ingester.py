"""Bridge between the frozen signal engine and the existing production API.

The engine is the strategy source of truth and is never modified. This module is
the integration shell around it: it converts the engine's per-bar decision into
the payload the app's signal API already accepts, and guarantees that an
execution-critical signal cannot be silently lost.

    engine  ->  THIS BRIDGE  ->  /api/public/engine/ingest/signal
                                 -> existing multi-tenant executor
                                 -> customer Binance USD-M Futures accounts

Nothing here holds Binance credentials, sizes a position, sets allocation or
leverage, or places an order. The executor remains solely responsible for all of
that, and no field carrying leverage or capital is ever forwarded.

HTTP MUST NOT LIVE IN THE ENGINE
--------------------------------
The engine's own `_startup_no_order_scan` walks every function in its module
globals and fails startup if it finds `requests.post(`, `requests.put(`,
`requests.delete(`, `requests.patch(`, `/fapi/v1/order` or `/api/v3/order`. The
functions this module patches INTO the engine are scanned too, so they delegate
to helpers defined here and contain no such literal. That check is the engine's
paper-only guarantee; keeping it satisfied is a hard constraint, not a style
preference.

TWO-PHASE DURABILITY
--------------------
`persist_bar_audits_and_state` ends with `save_runtime_state`, which is where the
engine's `last_processed_bar` becomes durable. So:

    prepare()            -> row is PREPARED, committed to SQLite
    original(...)        -> ... -> save_runtime_state   (engine state durable)
    promote()            -> row becomes READY; only now may the sender deliver

The sender never sees a PREPARED row, so a signal cannot reach the executor for
a bar the engine has not durably committed. And because a PREPARED row is never
deleted, a crash before durability cannot lose the signal either: the bridge
recognises the surviving row on the next pass and completes the interrupted
transaction, whatever processing mode that bar is then classified as.

FAILURE ASYMMETRY
-----------------
A local durability failure (SQLite) happens before the engine's state advances
and RAISES, so `run_once` rolls the bar back and retries it. An HTTP failure
happens on the sender thread and can never reach the engine at all. The outbox
commit is the transaction boundary between strategy state and delivery.
"""
from __future__ import annotations

import atexit
import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from outbox import KIND_SIGNAL, Outbox, canonical_json

log = logging.getLogger("ingester")

API_BASE = (os.environ.get("APP_API_BASE", "") or os.environ.get("LOVABLE_API_BASE", "")).rstrip("/")
SERVICE_TOKEN = os.environ.get("ENGINE_SERVICE_TOKEN", "")
USER_ID = os.environ.get("ENGINE_USER_ID", "")
TIMEOUT = float(os.environ.get("INGEST_TIMEOUT", "10"))
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "60"))
OUTBOX_DB_PATH = os.environ.get("OUTBOX_DB_PATH", "/app/outbox/outbox.db")

SIGNAL_PATH = "/api/public/engine/ingest/signal"
TRADE_PATH = "/api/public/engine/ingest/trade"
HEARTBEAT_PATH = "/api/public/engine/heartbeat"

LIVE_SCHEDULED = "LIVE_SCHEDULED"

# Strict allowlist. Every field the app's Zod schema accepts and nothing else.
# `t` is renamed to `bar_time`; `trade_id` is derived. Building the payload by
# allowlist rather than by deleting keys is deliberate: the engine injects
# conservative_*/middle_*/aggressive_* leverage and capital columns straight
# into the same event dict, and a denylist would leak any scenario added later.
SIGNAL_FIELDS = (
    "bar_closed_now",
    "valid_next_entry",
    "rule_side",
    "rule_reason",
    "ml_prob",
    "ml_threshold",
    "ml_accept",
    "opened",
    "closed_reason",
    "position_before",
    "position_after",
)

# Never forwarded, for the avoidance of doubt. Asserted by the tests.
FORBIDDEN_SIGNAL_FIELDS = (
    "run_id",
    "exit_px",
    "position_before_state",
    "position_after_state",
    "closed_position_state",
    "candidate_evaluation_blocked_by_position",
    "leverage_scenarios_json",
    "sample_features",
    "candidate_evaluations",
    "pre_ml_selector",
    "rule_funnel",
)

TRADE_FIELDS = (
    "trade_id",
    "side",
    "setup_name",
    "signal_t",
    "entry_t",
    "exit_t",
    "entry",
    "exit",
    "tp",
    "final_stop",
    "atr",
    "bars_held",
    "prob",
    "threshold",
    "exit_reason",
    "round_trip_cost",
)
TRADE_RENAMES = {
    "initial_sl": "sl",
    "net_pnl_rate_after_round_trip_cost": "net_pnl_rate",
}
_DATETIME_KEYS = ("bar_time", "signal_t", "entry_t", "exit_t")

_session = requests.Session()
_post_lock = threading.Lock()


class IngesterRecoveryError(RuntimeError):
    """Raised when a recovered bar rebuilds to a different payload.

    Fail closed: the row is HELD, nothing is delivered, and the engine is not
    allowed to advance past the bar. Advancing would strand the row behind a
    `last_processed_bar` that has moved on, and the next startup would then
    promote a payload we have just proven wrong.
    """


# ---------------------------------------------------------------------- #
# value normalisation (reused from the previous-generation worker)
# ---------------------------------------------------------------------- #

def _scalar(value: Any) -> Any:
    """Coerce numpy scalars to native Python; JSON cannot encode them."""
    if hasattr(value, "item") and not isinstance(value, (str, bytes, bool, int, float)):
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            return value
    return value


def _clean(value: Any) -> Any:
    """NaN/inf -> None. JSON has no encoding for them and Zod rejects null,
    so the caller drops the key entirely and `.optional()` accepts the absence."""
    value = _scalar(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _to_iso_z(value: Any) -> Any:
    """Strict ISO-8601 UTC with a 'Z' suffix. Zod's .datetime() rejects
    '+00:00' offsets. Unparseable values pass through untouched."""
    if not isinstance(value, str):
        return value
    from datetime import datetime, timezone

    s = value.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        base += "." + dt.strftime("%f")[:3]
    return base + "Z"


def _epoch_ms(value: Any) -> Optional[int]:
    """Milliseconds since epoch, for grounded bar comparison in the outbox."""
    if not isinstance(value, str):
        return None
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------------- #
# payload construction
# ---------------------------------------------------------------------- #

def _trade_id_from_event(event: Dict[str, Any]) -> Optional[str]:
    """Only the approved state fields are consulted, and only for this one id.

    Cosmetic: `signals.pending.ts` does not select trade_id, so the executor
    never sees it. The open position is preferred because it is the live one.
    """
    for key in ("position_after_state", "closed_position_state"):
        state = event.get(key)
        if isinstance(state, dict):
            tid = state.get("trade_id")
            if tid:
                return str(tid)
    return None


def build_signal_payload(event: Dict[str, Any]) -> Dict[str, Any]:
    """Allowlist-only payload for /ingest/signal."""
    out: Dict[str, Any] = {"bar_time": _to_iso_z(event.get("t"))}
    for key in SIGNAL_FIELDS:
        value = _clean(event.get(key))
        if value is not None:
            out[key] = value
    trade_id = _trade_id_from_event(event)
    if trade_id:
        out["trade_id"] = trade_id
    return out


def build_trade_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    """Allowlist + two renames for /ingest/trade.

    Everything the schema does not declare is dropped, including
    leverage_scenarios_json — this service never forwards capital or leverage.
    """
    out: Dict[str, Any] = {}
    for key in TRADE_FIELDS:
        value = _clean(row.get(key))
        if value is not None:
            out[key] = value
    for src, dst in TRADE_RENAMES.items():
        value = _clean(row.get(src))
        if value is not None:
            out[dst] = value
    for key in _DATETIME_KEYS:
        if key in out:
            out[key] = _to_iso_z(out[key])
    return out


def payload_diff(expected: Dict[str, Any], actual: Dict[str, Any]) -> List[str]:
    """Human-readable per-field diff, with a delta for numeric fields.

    Exists so an operator can tell a last-ulp float difference from a genuine
    decision divergence at a glance. No tolerance is applied anywhere.
    """
    lines: List[str] = []
    for key in sorted(set(expected) | set(actual)):
        a, b = expected.get(key, "<absent>"), actual.get(key, "<absent>")
        if a == b:
            continue
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            lines.append(f"{key}: stored={a!r} rebuilt={b!r} delta={b - a!r}")
        else:
            lines.append(f"{key}: stored={a!r} rebuilt={b!r}")
    return lines


# ---------------------------------------------------------------------- #
# transport
# ---------------------------------------------------------------------- #

def post_once(path: str, payload: Dict[str, Any]) -> Tuple[bool, Optional[int], Optional[str]]:
    """One attempt. Returns (ok, status_code, error) — never raises.

    The outbox drives retries for the signal lane; heartbeat and trade posting
    use the fire-and-forget wrapper below.
    """
    if not API_BASE or not SERVICE_TOKEN:
        return False, None, "missing APP_API_BASE/ENGINE_SERVICE_TOKEN"
    body = dict(payload)
    # ENGINE_USER_ID is optional. Set, it pins every post to one client. Unset,
    # the post carries no user_id and the app fans it out to every running
    # client, which is what lets a client who signed up minutes ago receive the
    # stream with no operator action.
    if USER_ID:
        body["user_id"] = USER_ID
    try:
        with _post_lock:
            r = _session.post(
                f"{API_BASE}{path}",
                json=body,
                headers={
                    "Authorization": f"Bearer {SERVICE_TOKEN}",
                    "Content-Type": "application/json",
                },
                timeout=TIMEOUT,
            )
    except Exception as exc:  # noqa: BLE001
        return False, None, repr(exc)
    if 200 <= r.status_code < 300:
        return True, r.status_code, None
    return False, r.status_code, (r.text or "")[:200]


def post_best_effort(path: str, payload: Dict[str, Any]) -> bool:
    """Single attempt, all failures swallowed. Never used for signals."""
    ok, status, error = post_once(path, payload)
    if not ok:
        log.warning("[ingest] %s failed (not retried): %s %s", path, status, error)
    return ok


# ---------------------------------------------------------------------- #
# the bridge
# ---------------------------------------------------------------------- #

class Bridge:
    """Owns the outbox, the patches, the heartbeat and the sender."""

    def __init__(
        self,
        outbox: Optional[Outbox] = None,
        poster: Optional[Callable[..., Tuple[bool, Optional[int], Optional[str]]]] = None,
        trade_poster: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        db_path: Optional[str] = None,
    ) -> None:
        if outbox is None:
            path = Path(db_path or OUTBOX_DB_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            outbox = Outbox(str(path), poster=poster or post_once)
        self.outbox = outbox
        self.trade_poster = trade_poster or post_best_effort
        self.last_position = "FLAT"
        self._pending_trades: List[Dict[str, Any]] = []
        self._heartbeat_started = False

    # -------------------------------------------------------------- #
    # patched engine entry points
    # -------------------------------------------------------------- #

    def persist_hook(self, original: Callable[..., Any]) -> Callable[..., Any]:
        bridge = self

        def patched(row, event, state, live_price, cycle_diagnostics, processing_mode):
            return bridge._on_persist(
                original, row, event, state, live_price, cycle_diagnostics, processing_mode
            )

        return patched

    def trade_hook(self, original: Callable[[Dict[str, Any]], bool]) -> Callable[..., bool]:
        bridge = self

        def patched(row):
            appended = original(row)
            if appended:
                bridge._stash_trade(row)
            return appended

        return patched

    # -------------------------------------------------------------- #
    # core sequencing
    # -------------------------------------------------------------- #

    def _on_persist(
        self, original, row, event, state, live_price, cycle_diagnostics, processing_mode
    ):
        bar_key = _to_iso_z(event.get("t"))
        bar_ms = _epoch_ms(event.get("t"))
        self._pending_trades = []

        position_after = event.get("position_after")
        if position_after in ("FLAT", "LONG", "SHORT"):
            self.last_position = position_after

        # A read failure here is a local durability fault BEFORE the engine's
        # state advances, so it must fail the bar rather than silently continue.
        prepared = self.outbox.get(bar_key)

        if prepared is not None:
            return self._recover_interrupted(
                original, prepared, bar_key, bar_ms, row, event, state,
                live_price, cycle_diagnostics, processing_mode,
            )

        if processing_mode != LIVE_SCHEDULED:
            # Ordinary CATCHUP: no signal is prepared and none is emitted. The
            # orphan sweep still runs, so an old PREPARED row is detected even
            # on a pass that produces nothing of its own.
            result = original(row, event, state, live_price, cycle_diagnostics, processing_mode)
            self._pending_trades = []
            self._sweep(bar_ms, None)
            return result

        payload = build_signal_payload(event)
        # Durability point. A failure raises, run_once rolls the bar back, and
        # the bar is retried — the engine cannot advance past a LIVE bar whose
        # signal is not already committed.
        self.outbox.prepare(bar_key, bar_ms, SIGNAL_PATH, payload)

        result = original(row, event, state, live_price, cycle_diagnostics, processing_mode)
        # Engine state is durable from here on: nothing below may raise.
        self._promote(bar_key)
        self._flush_trades()
        self._sweep(bar_ms, bar_key)
        return result

    def _recover_interrupted(
        self, original, prepared, bar_key, bar_ms, row, event, state,
        live_price, cycle_diagnostics, processing_mode,
    ):
        """Complete an interrupted LIVE transaction, whatever the current mode.

        A surviving PREPARED row proves this bar was previously classified
        LIVE_SCHEDULED, so ordinary CATCHUP suppression must not apply to it.
        """
        rebuilt = build_signal_payload(event)
        if canonical_json(rebuilt) != prepared.payload:
            self.outbox.hold(bar_key, "payload divergence on recovery")
            try:
                stored = json.loads(prepared.payload)
            except Exception:  # noqa: BLE001
                stored = {}
            log.error(
                "[ingest] RECOVERY PAYLOAD DIVERGENCE | bar_time=%s | HELD, not delivered, "
                "engine not advanced | diff: %s",
                bar_key,
                "; ".join(payload_diff(stored, rebuilt)) or "<unprintable>",
            )
            raise IngesterRecoveryError(
                f"payload divergence on recovery for bar_time={bar_key}"
            )

        log.warning(
            "[ingest] recovering interrupted LIVE bar | bar_time=%s | mode=%s",
            bar_key,
            processing_mode,
        )
        result = original(row, event, state, live_price, cycle_diagnostics, processing_mode)
        self._promote(bar_key)
        self._flush_trades()
        self._sweep(bar_ms, bar_key)
        return result

    # -------------------------------------------------------------- #
    # post-durability steps: none of these may raise
    # -------------------------------------------------------------- #

    def _promote(self, bar_key: str) -> None:
        try:
            if not self.outbox.promote(bar_key):
                log.error("[ingest] promote found no PREPARED row | bar_time=%s", bar_key)
        except Exception as exc:  # noqa: BLE001
            # The engine's state is already durable; raising would roll back a
            # bar the engine has committed. Startup recovery promotes this row
            # instead, because its bar is now <= last_processed_bar.
            log.error("[ingest] promote failed | bar_time=%s | %s", bar_key, exc)

    def _sweep(self, bar_ms: Optional[int], exclude_key: Optional[str]) -> None:
        if bar_ms is None:
            return
        try:
            self.outbox.detect_skipped_orphans(bar_ms, exclude_event_key=exclude_key)
        except Exception as exc:  # noqa: BLE001
            log.error("[ingest] orphan sweep failed: %s", exc)

    def _stash_trade(self, row: Dict[str, Any]) -> None:
        try:
            self._pending_trades.append(build_trade_payload(dict(row)))
        except Exception as exc:  # noqa: BLE001
            log.warning("[ingest] trade payload build failed: %s", exc)

    def _flush_trades(self) -> None:
        """Phase 1 trade reporting: best effort, single attempt, no retry.

        Deliberately outside the outbox. `/ingest/trade` has no idempotency, so
        a retry would duplicate history rows. A dropped post costs one dashboard
        row; every closed trade is still durably in the engine's own
        shadow_live_trades.csv and can be backfilled later.
        """
        trades, self._pending_trades = self._pending_trades, []
        for trade in trades:
            try:
                self.trade_poster(TRADE_PATH, trade)
            except Exception as exc:  # noqa: BLE001
                log.warning("[ingest] trade post failed (not retried): %s", exc)

    # -------------------------------------------------------------- #
    # heartbeat
    # -------------------------------------------------------------- #

    def heartbeat_payload(self) -> Dict[str, Any]:
        status, message = "running", "engine alive"
        try:
            degraded = self.outbox.degraded(KIND_SIGNAL)
        except Exception as exc:  # noqa: BLE001
            degraded = {"reason": "outbox unreadable", "detail": str(exc), "event_key": None}
        if degraded:
            status = "error"
            message = (
                f"signal lane {degraded.get('reason')}: "
                f"{degraded.get('event_key')} ({degraded.get('detail')})"
            )[:500]
        return {
            "status": status,
            "current_position": self.last_position,
            "message": message,
        }

    def _heartbeat_loop(self) -> None:
        while True:
            try:
                post_best_effort(HEARTBEAT_PATH, self.heartbeat_payload())
            except Exception as exc:  # noqa: BLE001
                log.warning("[heartbeat] error: %s", exc)
            time.sleep(HEARTBEAT_INTERVAL)

    def _shutdown_heartbeat(self) -> None:
        try:
            post_best_effort(
                HEARTBEAT_PATH,
                {
                    "status": "stopped",
                    "current_position": self.last_position,
                    "message": "engine shutdown",
                },
            )
        except Exception:  # noqa: BLE001
            pass

    def start_heartbeat(self) -> None:
        if self._heartbeat_started:
            return
        self._heartbeat_started = True
        threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True).start()
        atexit.register(self._shutdown_heartbeat)
        log.info("[heartbeat] started (interval=%ss)", HEARTBEAT_INTERVAL)

    # -------------------------------------------------------------- #
    # attachment
    # -------------------------------------------------------------- #

    def recover_from_state_file(self, state_file: Any) -> Dict[str, int]:
        """Resolve PREPARED rows against the engine's own persisted state.

        `last_processed_bar` is written by save_runtime_state via an atomic
        replace, so it is never torn. Comparing two durable values is what makes
        recovery independent of timing.
        """
        last_ms = None
        try:
            path = Path(str(state_file))
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                last_ms = _epoch_ms(data.get("last_processed_bar"))
        except Exception as exc:  # noqa: BLE001
            log.error("[ingest] could not read engine state for recovery: %s", exc)
        result = self.outbox.recover(last_ms)
        log.info(
            "[ingest] outbox recovery | last_processed_bar_ms=%s | promoted=%d kept=%d",
            last_ms,
            result["promoted"],
            result["kept"],
        )
        return result

    def attach(self, live_code_module: Any, start_threads: bool = True) -> None:
        self.recover_from_state_file(getattr(live_code_module, "RUNTIME_STATE_FILE", ""))

        live_code_module.persist_bar_audits_and_state = self.persist_hook(
            live_code_module.persist_bar_audits_and_state
        )
        live_code_module._append_shadow_trade_row = self.trade_hook(
            live_code_module._append_shadow_trade_row
        )

        if start_threads:
            self.start_heartbeat()
            self.outbox.start_sender()

        log.info(
            "[ingest] attached | base=%s user=%s | signal lane durable, "
            "trade reporting best-effort",
            API_BASE or "(none)",
            USER_ID or "(broadcast: every running client)",
        )


_BRIDGE: Optional[Bridge] = None


def attach_ingester(live_code_module: Any) -> Bridge:
    """Production entry point, called once from main.py before live_code.main()."""
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = Bridge()
    _BRIDGE.attach(live_code_module)
    return _BRIDGE
