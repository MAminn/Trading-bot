"""Pure-logic RiskGuard tests. No network, no credentials, stdlib only.

The guard no longer holds a sizing mode or a notional ceiling of its own. What
remains is the exchange's constraints (symbol, minimum notional, leverage
maximum) plus the operator's explicit per-order live cap and the ordinary
one-position / min-interval rules.
"""

import inspect
import logging
import time

import pytest

import risk_guard as risk_guard_module
from risk_guard import RiskGuard


def guard(max_leverage=90):
    return RiskGuard(max_leverage=max_leverage)


def open_order(notional, leverage=30, qty=1.0, side="LONG"):
    return {
        "symbol": "ETHUSDT",
        "intent": "OPEN",
        "side": side,
        "qty": qty,
        "notional_usd": notional,
        "leverage": leverage,
    }


def close_order(side="LONG", qty=1.0):
    return {
        "symbol": "ETHUSDT",
        "intent": "CLOSE",
        "side": side,
        "qty": qty,
        "notional_usd": 0.0,
    }


# --- no internal notional ceiling remains -------------------------------- #

def test_no_absolute_max_notional_constant():
    """ABSOLUTE_MAX_NOTIONAL_USD was 500 and would clamp a correctly-sized
    $1,800 order. It is gone, and nothing may reintroduce it."""
    assert not hasattr(RiskGuard, "ABSOLUTE_MAX_NOTIONAL_USD")
    src = inspect.getsource(risk_guard_module)
    assert "ABSOLUTE_MAX_NOTIONAL_USD" not in src
    assert "max_notional_usd" not in src
    assert "live_cap" not in src
    assert "sizing_mode" not in src
    assert "full_capital" not in src


def test_no_sizing_mode_api():
    assert not hasattr(RiskGuard, "set_sizing_mode")
    assert not hasattr(risk_guard_module, "SIZING_MODES")


@pytest.mark.parametrize("notional", [600, 1800, 3000, 50_000, 1_000_000])
def test_large_notionals_pass(notional):
    """Only exchange constraints apply, and a large notional is not one of
    them. Whatever the wallet x allocation x leverage produced is what runs."""
    allowed, reason = guard().evaluate(open_order(notional), 0, None)
    assert allowed is True, reason


def test_1800_notional_is_allowed():
    """The specification's worked example: $300 wallet, 20%, 30x."""
    allowed, reason = guard().evaluate(open_order(1800), 0, None)
    assert allowed is True, reason


# --- exchange minimum notional ------------------------------------------ #

def test_rejects_below_min_notional():
    allowed, reason = guard().evaluate(open_order(10), 0, None)
    assert allowed is False
    assert reason == "below min notional"
    assert RiskGuard.MIN_NOTIONAL_USD == 20


# --- no operator dollar cap remains -------------------------------------- #

def test_the_guard_takes_no_live_cap():
    """A per-order dollar ceiling here would reject the client's own configured
    size. Size is decided once, from the wallet balance, in signal_consumer."""
    with pytest.raises(TypeError):
        RiskGuard(max_leverage=90, live_cap_usd=30)
    assert not hasattr(RiskGuard(max_leverage=90), "set_live_cap")
    src = inspect.getsource(risk_guard_module)
    assert "live_cap" not in src
    assert "LIVE_ORDER_CAP" not in src


def test_the_only_notional_test_is_the_exchange_minimum():
    """One lower bound, no upper bound."""
    src = inspect.getsource(RiskGuard.evaluate)
    assert "notional < self.MIN_NOTIONAL_USD" in src
    assert "notional >" not in src


# --- exchange leverage maximum ------------------------------------------ #

def test_rejects_leverage_above_exchange_max():
    g = guard(max_leverage=50)
    allowed, reason = g.evaluate(open_order(1800, leverage=90), 0, None)
    assert allowed is False
    assert "exceeds exchange max" in reason


def test_permits_leverage_at_exchange_max():
    g = guard(max_leverage=90)
    allowed, reason = g.evaluate(open_order(1800, leverage=90), 0, None)
    assert allowed is True, reason


def test_update_limits_sets_max_leverage_from_the_bracket_probe():
    g = guard(max_leverage=1)
    allowed, _ = g.evaluate(open_order(1800, leverage=30), 0, None)
    assert allowed is False
    g.update_limits(max_leverage=90)
    allowed, reason = g.evaluate(open_order(1800, leverage=30), 0, None)
    assert allowed is True, reason


# --- position and interval rules ---------------------------------------- #

def test_rejects_open_when_a_position_is_already_open():
    allowed, reason = guard().evaluate(open_order(1800), 0.5, None)
    assert allowed is False
    assert reason == "position already open"


def test_rejects_open_inside_the_min_interval():
    g = guard()
    allowed, reason = g.evaluate(open_order(1800), 0, time.time())
    assert allowed is False
    assert reason == "min order interval not elapsed"


def test_allows_open_after_the_min_interval():
    g = guard()
    allowed, reason = g.evaluate(
        open_order(1800), 0, time.time() - RiskGuard.MIN_ORDER_INTERVAL_SECONDS - 1
    )
    assert allowed is True, reason


def test_rejects_non_positive_qty():
    allowed, reason = guard().evaluate(open_order(1800, qty=0), 0, None)
    assert allowed is False
    assert reason == "qty must be positive"


# --- CLOSE stays exempt -------------------------------------------------- #

def test_close_long_allowed_with_matching_position():
    allowed, reason = guard().evaluate(close_order("LONG"), 1.0, None)
    assert allowed is True, reason


def test_close_short_allowed_with_matching_position():
    allowed, reason = guard().evaluate(close_order("SHORT"), -1.0, None)
    assert allowed is True, reason


def test_close_rejected_without_a_matching_position():
    allowed, reason = guard().evaluate(close_order("LONG"), 0, None)
    assert allowed is False
    assert reason == "no matching position to close"


def test_close_is_exempt_from_the_min_interval():
    """A position that is already open must stay closable whatever else is set."""
    allowed, reason = guard().evaluate(close_order("LONG"), 1.0, time.time())
    assert allowed is True, reason


# --- symbol allowlist ---------------------------------------------------- #

def test_symbol_allowlist_unchanged():
    order = open_order(100)
    order["symbol"] = "BTCUSDT"
    allowed, reason = guard().evaluate(order, 0, None)
    assert allowed is False
    assert "not allowed" in reason
