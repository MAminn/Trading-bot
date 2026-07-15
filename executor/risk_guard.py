"""Hardcoded risk guard for trade-capable modes.

Pure logic: no network calls, stdlib only. Every intended order must pass
`evaluate()` before it may be persisted as an allowed intent.
"""

import time


class RiskGuard:
    ALLOWED_SYMBOLS = {"ETHUSDT"}
    MAX_NOTIONAL_USD = 100
    MIN_NOTIONAL_USD = 20
    MAX_LEVERAGE = 1  # recorded for reference; enforced at startup via account config
    MIN_ORDER_INTERVAL_SECONDS = 60

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

        # OPEN: all existing checks, unchanged.
        if qty <= 0:
            return False, "qty must be positive"

        try:
            notional = float(intended_order.get("notional_usd") or 0)
        except (TypeError, ValueError):
            notional = 0.0
        if notional > self.MAX_NOTIONAL_USD:
            return False, f"notional {notional} exceeds max {self.MAX_NOTIONAL_USD}"
        if notional < self.MIN_NOTIONAL_USD:
            return False, "below min notional"

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
