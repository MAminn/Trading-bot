"""Executor telemetry: reports what this process is actually doing to the app.

Strictly one-way and strictly best-effort. Nothing here reads a control value,
gates an order, or influences sizing — the executor's live capability still
comes exclusively from its own environment. A missing endpoint, a missing
table, an auth failure or a dead network must therefore never interrupt a
cycle, so every failure path is swallowed after logging.

No key material is ever reported. `keys_present` is a boolean; the key itself
is never read here, and the permission status is derived from whether an
already-performed signed call succeeded — this module makes no Binance calls.
"""

import logging
from datetime import datetime, timezone

import requests

from binance_client import BinanceAPIError
from reconciler import FLAT_EPSILON

log = logging.getLogger("executor.status")

REQUEST_TIMEOUT_SECONDS = 10
INGEST_PATH = "/api/public/engine/ingest/executor_status"

# Binance error codes that mean the credential itself was rejected, as opposed
# to a transient or unrelated API failure. Anything else stays 'unknown': a
# permission claim we cannot substantiate is worse than no claim.
AUTH_FAILURE_CODES = frozenset({-2008, -2014, -2015, -1022})

# Log the first failure loudly, then stay quiet until it recovers. A permanently
# unreachable telemetry sink must not drown the trading log it sits next to.
_LOG_EVERY_N_FAILURES = 60


def position_side(position_amt) -> str | None:
    """FLAT/LONG/SHORT for a signed position amount, or None if unreadable.

    Uses the reconciler's flat threshold so 'FLAT' means the same thing in the
    dashboard as it does in the reconcile decision."""
    try:
        amt = float(position_amt or 0)
    except (TypeError, ValueError):
        return None
    if abs(amt) < FLAT_EPSILON:
        return "FLAT"
    return "LONG" if amt > 0 else "SHORT"


def permission_status_for_error(exc: Exception) -> str:
    """'failed' only when Binance rejected the credential itself."""
    if isinstance(exc, BinanceAPIError) and exc.code in AUTH_FAILURE_CODES:
        return "failed"
    return "unknown"


def _as_float(value):
    """Best-effort float, or None. Telemetry never invents a 0 for unknown."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_snapshot(
    mode: str,
    env_mode_ceiling: str,
    account: dict | None,
    positions: list[dict] | None,
    reconcile: dict | None,
    keys_present: bool | None,
    permission_status: str | None,
    message: str | None,
    db_execution_mode: str | None = None,
    auto_execute_enabled: bool | None = None,
    orders_enabled: bool | None = None,
    blocked_reason: str | None = None,
) -> dict:
    """Assemble one telemetry payload. Pure: no I/O, no exchange calls.

    Every unknown value is reported as None rather than omitted, so a
    successful heartbeat always overwrites the previous snapshot in full and
    stale readings can never masquerade as current ones."""
    pos = (positions or [{}])[0] if positions else {}
    amt = _as_float(pos.get("positionAmt"))

    snapshot = {
        # Three separate facts, deliberately reported separately: what the
        # database asked for, what this host permits, and what is actually
        # running. Collapsing them is how a degraded request goes unnoticed.
        "effective_mode": mode,
        "env_mode_ceiling": env_mode_ceiling,
        "db_execution_mode": db_execution_mode,
        "auto_execute_enabled": auto_execute_enabled,
        "orders_enabled": orders_enabled,
        "blocked_reason": blocked_reason,
        "wallet_balance_usd": _as_float((account or {}).get("totalWalletBalance")),
        "available_balance_usd": _as_float((account or {}).get("availableBalance")),
        "position_amt": amt,
        "position_side": position_side(amt) if amt is not None else None,
        "entry_price": _as_float(pos.get("entryPrice")),
        "position_leverage": _as_float(pos.get("leverage")),
        "margin_type": str(pos.get("marginType"))[:30] if pos.get("marginType") else None,
        "reconcile_match": None,
        "reconcile_expected": None,
        "reconcile_actual": None,
        "last_reconcile_at": None,
        "keys_present": keys_present,
        "permission_status": permission_status,
        "message": message[:500] if message else None,
    }

    if reconcile is not None:
        snapshot["reconcile_match"] = bool(reconcile.get("match"))
        snapshot["reconcile_expected"] = _as_float(reconcile.get("expected"))
        snapshot["reconcile_actual"] = _as_float(reconcile.get("actual"))
        at = reconcile.get("at")
        snapshot["last_reconcile_at"] = at if isinstance(at, str) else None

    return snapshot


def utc_now_z() -> str:
    """Canonical UTC ISO 8601 with a Z suffix — the app's Zod validator rejects
    +00:00 offsets."""
    return (
        datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
    )


class StatusReporter:
    """POSTs telemetry to the app. Every failure is contained here."""

    def __init__(
        self, app_api_base: str, engine_service_token: str, user_id: str | None = None
    ):
        self._url = f"{app_api_base.rstrip('/')}{INGEST_PATH}"
        # The default recipient, for a single-user executor. A multi-tenant loop
        # leaves this None and names the user on every report() instead, so one
        # reporter can serve many sessions without ever holding a user's state.
        self._user_id = user_id
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {engine_service_token}"}
        )
        self._failures = 0

    def report(self, snapshot: dict, user_id: str | None = None) -> bool:
        """Send one snapshot. Returns True on success. Never raises: telemetry
        is not permitted to fail a trading cycle.

        `user_id` names the user this snapshot describes. It is required when
        the reporter was constructed without a default — a multi-tenant loop
        shares one reporter across sessions, and a snapshot posted to the wrong
        user would show one client another client's balance and position.
        """
        target = user_id or self._user_id
        if not target:
            # Refusing is the only safe option: there is no correct user to
            # attribute this snapshot to, and guessing would misattribute it.
            self._note_failure("no user_id for snapshot")
            return False
        payload = {"user_id": target, **snapshot}
        try:
            resp = self._session.post(
                self._url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except (OSError, ValueError) as exc:
            self._note_failure(f"POST failed: {exc}")
            return False
        if not 200 <= resp.status_code < 300:
            # 404 here means the app predates this endpoint; 500 can mean the
            # migration has not been applied yet. Both are survivable.
            self._note_failure(f"HTTP {resp.status_code}")
            return False
        if self._failures:
            log.info("executor_status telemetry recovered after %d failures", self._failures)
        self._failures = 0
        return True

    def _note_failure(self, detail: str) -> None:
        self._failures += 1
        if self._failures == 1 or self._failures % _LOG_EVERY_N_FAILURES == 0:
            log.warning(
                "executor_status telemetry unavailable (%s) | failures=%d | "
                "trading is unaffected",
                detail,
                self._failures,
            )
