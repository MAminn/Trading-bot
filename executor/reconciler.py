"""Reconciler: compares the app's record of the last executed order against the
live Binance position to decide whether new OPENs are safe to place.

Fetches GET {app_api_base}/api/public/engine/orders/state (bearer auth via the
supplied session) and, given the live Binance position amount, derives:

  * expected  — signed expected position from last_executed (LONG positive,
                SHORT negative; 0.0 when flat: last intent CLOSE or none).
  * match     — whether the actual Binance amount agrees with expected.
  * is_running / stale_intents — passed through from the endpoint.

No Binance access lives here; the caller supplies the already-fetched position
amount. This module never places orders.
"""

import logging

log = logging.getLogger("executor.reconciler")

REQUEST_TIMEOUT_SECONDS = 10

# A position is considered flat below one step size.
FLAT_EPSILON = 0.001
# Open-quantity match tolerance: one step size of rounding slack.
QTY_TOLERANCE = 0.002


class ReconcilerError(Exception):
    """Raised when orders/state cannot be fetched or parsed."""


def expected_signed_amount(last_executed) -> float:
    """Signed expected position amount from last_executed.

    OPEN -> LONG positive / SHORT negative absolute qty. CLOSE or None -> 0.0
    (flat)."""
    if not last_executed:
        return 0.0
    if last_executed.get("intent") != "OPEN":
        return 0.0
    qty = abs(float(last_executed.get("qty") or 0))
    return qty if last_executed.get("side") == "LONG" else -qty


def position_matches(expected: float, position_amt) -> bool:
    """True when the actual position agrees with the signed expected amount.

    Flat means abs(amt) < FLAT_EPSILON. Open means the sign matches and the
    magnitude is within QTY_TOLERANCE."""
    amt = float(position_amt or 0)
    if expected == 0.0:
        return abs(amt) < FLAT_EPSILON
    # Sign must match: expected LONG needs amt > 0, expected SHORT needs amt < 0.
    if (expected > 0) != (amt > 0):
        return False
    return abs(abs(amt) - abs(expected)) <= QTY_TOLERANCE


class Reconciler:
    def __init__(self, app_api_base: str, session, user_id: str):
        self._base = app_api_base.rstrip("/")
        # requests.Session pre-loaded with the Authorization bearer header.
        self._session = session
        self._user_id = user_id

    def _fetch_state(self) -> dict:
        try:
            resp = self._session.get(
                f"{self._base}/api/public/engine/orders/state",
                params={"user_id": self._user_id},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            raise ReconcilerError(f"GET orders/state failed: {exc}") from exc
        if not 200 <= resp.status_code < 300:
            raise ReconcilerError(f"GET orders/state -> HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ReconcilerError(f"orders/state returned non-JSON: {exc}") from exc

    def reconcile(self, position_amt) -> dict:
        """Fetch state and compare against the live position amount. Returns the
        state object: expected (signed float), match (bool), is_running (bool),
        stale_intents (list[str]). Raises ReconcilerError on fetch/parse failure."""
        state = self._fetch_state()
        expected = expected_signed_amount(state.get("last_executed"))
        return {
            "expected": expected,
            "match": position_matches(expected, position_amt),
            "is_running": bool(state.get("is_running", True)),
            "stale_intents": list(state.get("stale_intents") or []),
        }
