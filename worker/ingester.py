"""
Bridge between the frozen Python engine and the app HTTP API.

live_code.py writes signals/trades to local CSVs via `append_csv_row(path, row, columns)`.
We wrap that function so that, in addition to the local write, each new row is
POSTed to one of the app `/api/public/engine/ingest/*` endpoints.

Mapping (by filename):
  *signal_monitor*  -> POST /api/public/engine/ingest/signal
  *trade_log*       -> POST /api/public/engine/ingest/trade

Open-position snapshots are derived from the engine's in-memory state and are
emitted from `process_one_signal_bar` via the trade_log + signal flow.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("ingester")

API_BASE = (os.environ.get("APP_API_BASE", "") or os.environ.get("LOVABLE_API_BASE", "")).rstrip("/")
SERVICE_TOKEN = os.environ.get("ENGINE_SERVICE_TOKEN", "")
USER_ID = os.environ.get("ENGINE_USER_ID", "")
TIMEOUT = float(os.environ.get("INGEST_TIMEOUT", "10"))

_session = requests.Session()
_lock = threading.Lock()


def _post(path: str, payload: Dict[str, Any]) -> None:
    if not API_BASE or not SERVICE_TOKEN or not USER_ID:
        log.debug("[ingest] skipped: missing API_BASE/TOKEN/USER_ID")
        return
    url = f"{API_BASE}{path}"
    payload = {**payload, "user_id": USER_ID}
    try:
        r = _session.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}", "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        if r.status_code >= 300:
            log.warning("[ingest] %s -> %s %s", path, r.status_code, r.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("[ingest] %s failed: %s", path, exc)


def _route_for_path(path_str: str) -> Optional[str]:
    p = path_str.lower()
    if "trade_log" in p:
        return "/api/public/engine/ingest/trade"
    if "signal_monitor" in p:
        return "/api/public/engine/ingest/signal"
    return None


def _normalize_row(row: Dict[str, Any], columns: Optional[List[str]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    src = row
    if columns:
        for c in columns:
            out[c] = src.get(c)
    else:
        out.update(src)
    # Make json serializable
    for k, v in list(out.items()):
        try:
            import math
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                out[k] = None
        except Exception:
            pass
    return out


_DATETIME_KEYS = ("bar_time", "signal_t", "entry_t", "exit_t")


def _to_iso_z(value: Any) -> Any:
    """Normalize a datetime string to strict ISO-8601 UTC with 'Z' suffix.

    Zod's .datetime() rejects '+00:00' offsets. Unparseable values pass through.
    """
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


def _transform_for_route(route: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Reshape a CSV row into the payload the app's Zod schemas accept."""
    out = dict(payload)
    if route.endswith("/signal"):
        if "t" in out:
            out["bar_time"] = out.pop("t")
        out.pop("event_json", None)
    elif route.endswith("/trade"):
        if "net_pnl_rate_after_round_trip_cost" in out:
            out["net_pnl_rate"] = out.pop("net_pnl_rate_after_round_trip_cost")
        out.pop("logged_at_utc", None)
        out.pop("leverage_scenarios_json", None)
    for k in _DATETIME_KEYS:
        if k in out:
            out[k] = _to_iso_z(out[k])
    # Zod .optional() fields reject null; missing keys are valid.
    return {k: v for k, v in out.items() if v is not None}


def attach_ingester(live_code_module) -> None:
    """Monkey-patch live_code.append_csv_row to mirror writes to Lovable."""
    original = live_code_module.append_csv_row

    def patched(path, row, columns=None):  # type: ignore[no-untyped-def]
        # Always preserve local CSV behaviour (auditing).
        original(path, row, columns)
        try:
            route = _route_for_path(str(path))
            if route is None:
                return
            with _lock:
                _post(route, _transform_for_route(route, _normalize_row(row, columns)))
        except Exception as exc:  # noqa: BLE001
            log.warning("[ingest] patch error: %s", exc)

    live_code_module.append_csv_row = patched
    log.info("[ingest] attached. base=%s user=%s", API_BASE or "(none)", USER_ID or "(none)")
