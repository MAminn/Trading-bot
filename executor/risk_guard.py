"""Hardcoded risk guard for trade-capable modes.

Pure logic: no network calls, stdlib only. Every intended order must pass
`evaluate()` before it may be persisted as an allowed intent.

What this guard does NOT do any more: it holds no sizing mode, no config-driven
notional ceiling, no absolute code ceiling and no operator dollar cap. Position
size comes from exactly one place — the user's real Binance wallet balance x
allocation x leverage, computed in signal_consumer — and any notional limit here
would silently shrink or reject a correctly-sized order. Every limit that
remains is either an exchange constraint (minimum notional, leverage maximum,
symbol) or a timing/one-position safety gate that says nothing about size.
"""

import logging
import time

log = logging.getLogger("executor.risk_guard")


class RiskGuard:
    ALLOWED_SYMBOLS = {"ETHUSDT"}
    # Binance's ETHUSDT minimum notional. An exchange constraint, not a policy.
    MIN_NOTIONAL_USD = 20
    MIN_ORDER_INTERVAL_SECONDS = 60

    def __init__(self, max_leverage=1):
        # Conservative default so the guard is safe before the bracket probe has
        # supplied the real exchange ceiling.
        self._max_leverage = max_leverage

    def update_limits(self, *, max_leverage=None) -> None:
        """Update only the limits supplied. main.py sets max_leverage after the
        bracket probe."""
        if max_leverage is not None:
            self._max_leverage = max_leverage

    def evaluate(
        self, intended_order: dict, current_position_amt, last_order_time
    ) -> tuple[bool, str]:
        """Return (allowed, reason). last_order_time is epoch seconds or None."""
        symbol = intended_order.get("symbol")
        if symbol not in self.ALLOWED_SYMBOLS:
            return False, f"symbol {symbol} not allowed"

        try:
            qty = float(intended_order.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0

        intent = intended_order.get("intent")

        # CLOSE is a reducing order: exempt from notional caps, the min-interval
        # rule, and the one-position rule. It only needs a real position to
        # reduce in the matching direction.
        if intent == "CLOSE":
            try:
                position_amt = float(current_position_amt or 0)
            except (TypeError, ValueError):
                return False, "unreadable current position"
            side = intended_order.get("side")
            matches = (side == "SHORT" and position_amt < 0) or (
                side == "LONG" and position_amt > 0
            )
            if not matches:
                return False, "no matching position to close"
            if qty <= 0:
                return False, "qty must be positive"
            return True, "ok"

        # OPEN.
        if qty <= 0:
            return False, "qty must be positive"

        try:
            notional = float(intended_order.get("notional_usd") or 0)
        except (TypeError, ValueError):
            notional = 0.0
        # The exchange minimum. Deliberately the ONLY notional test here: an
        # upper bound would be a sizing decision, and sizing is decided once, in
        # signal_consumer, from the wallet balance.
        if notional < self.MIN_NOTIONAL_USD:
            return False, "below min notional"

        try:
            order_leverage = float(intended_order.get("leverage") or 0)
        except (TypeError, ValueError):
            order_leverage = 0.0
        if order_leverage > float(self._max_leverage):
            return (
                False,
                f"leverage {intended_order.get('leverage')} exceeds exchange max "
                f"{self._max_leverage}",
            )

        try:
            position_amt = float(current_position_amt or 0)
        except (TypeError, ValueError):
            return False, "unreadable current position"
        if position_amt != 0:
            return False, "position already open"

        if (
            last_order_time is not None
            and time.time() - last_order_time < self.MIN_ORDER_INTERVAL_SECONDS
        ):
            return False, "min order interval not elapsed"

        return True, "ok"
