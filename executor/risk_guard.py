"""Hardcoded risk guard for trade-capable modes.

Pure logic: no network calls, stdlib only. Every intended order must pass
`evaluate()` before it may be persisted as an allowed intent.
"""

import logging
import time

log = logging.getLogger("executor.risk_guard")

SIZING_MODES = frozenset({"allocation", "full_capital"})


class RiskGuard:
    ALLOWED_SYMBOLS = {"ETHUSDT"}
    MIN_NOTIONAL_USD = 20
    MIN_ORDER_INTERVAL_SECONDS = 60
    # Absolute backstop in allocation mode: no config value may raise the
    # effective cap above this. Inapplicable in full_capital, where available
    # margin and the exchange bracket cap are the bounds instead.
    ABSOLUTE_MAX_NOTIONAL_USD = 500

    def __init__(self, max_notional_usd=100, max_leverage=1, live_cap_usd=None):
        # Conservative defaults so the guard is safe before the bracket probe
        # and the first config refresh have supplied real limits.
        self._max_notional_usd = max_notional_usd
        self._max_leverage = max_leverage
        # Fail closed to the capped mode until a refresh says otherwise.
        self._sizing_mode = "allocation"
        # LIVE_TRADE only: an absolute per-order notional ceiling from the
        # environment. Unlike _max_notional_usd it is NOT config-driven, cannot
        # be raised by a config refresh, and applies in EVERY sizing mode —
        # including full_capital, which has no internal ceiling of its own.
        self._live_cap_usd = None if live_cap_usd is None else float(live_cap_usd)
        # The value handed in at construction comes from .env and is this host's
        # hard ceiling. It is captured separately and never reassigned, so
        # set_live_cap() below can only ever narrow the effective cap.
        self._live_cap_ceiling = self._live_cap_usd

    def update_limits(self, *, max_notional_usd=None, max_leverage=None) -> None:
        """Update only the limits supplied. main.py sets max_leverage after the
        bracket probe; the consumer sets max_notional_usd on every refresh."""
        if max_notional_usd is not None:
            self._max_notional_usd = max_notional_usd
        if max_leverage is not None:
            self._max_leverage = max_leverage

    def set_live_cap(self, cap) -> None:
        """Apply a database-supplied per-order cap.

        Clamped to the ceiling captured at construction, so a config refresh can
        only ever lower the cap. A None from the database leaves the host
        ceiling in force rather than removing the cap: 'unspecified' must not
        read as 'unlimited'.
        """
        if cap is None:
            self._live_cap_usd = self._live_cap_ceiling
            return
        try:
            requested = float(cap)
        except (TypeError, ValueError):
            # Unreadable input never widens: fall back to the host ceiling.
            log.error("unreadable live cap %r — keeping host ceiling %s", cap, self._live_cap_ceiling)
            self._live_cap_usd = self._live_cap_ceiling
            return
        if self._live_cap_ceiling is None:
            self._live_cap_usd = requested
            return
        self._live_cap_usd = min(self._live_cap_ceiling, requested)

    def set_sizing_mode(self, mode: str) -> None:
        """Select which notional-ceiling rule applies to OPENs.

        An unrecognised value is treated as 'allocation' — the narrower of the
        two. The guard never widens on input it does not understand."""
        if mode in SIZING_MODES:
            self._sizing_mode = mode
            return
        self._sizing_mode = "allocation"
        log.error(
            "invalid sizing_mode=%r — falling back to allocation (capped)", mode
        )

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
        # The live cap is checked before, and independently of, the sizing-mode
        # rules — it binds in full_capital too, where no other notional ceiling
        # applies. Checked first so its rejection reason is the one reported.
        if self._live_cap_usd is not None and notional > self._live_cap_usd:
            return (
                False,
                f"notional {notional} exceeds live cap {self._live_cap_usd}",
            )
        # full_capital deliberately has no internal notional ceiling: it is
        # bounded by available margin and the exchange bracket cap instead.
        # Every other check below applies identically in both modes.
        if self._sizing_mode != "full_capital":
            effective_max = min(
                float(self._max_notional_usd), float(self.ABSOLUTE_MAX_NOTIONAL_USD)
            )
            if notional > effective_max:
                return False, f"notional {notional} exceeds max {effective_max}"
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
