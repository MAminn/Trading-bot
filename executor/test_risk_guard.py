"""Gate 5: pure-logic RiskGuard tests. No network, no credentials, stdlib only."""

import logging
import time

import pytest

from risk_guard import RiskGuard


def guard(max_notional_usd=500, max_leverage=90, mode="allocation"):
    g = RiskGuard(max_notional_usd=max_notional_usd, max_leverage=max_leverage)
    g.set_sizing_mode(mode)
    return g


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


# --- notional ceiling: mode-scoped -------------------------------------- #

def test_allocation_rejects_notional_600():
    allowed, reason = guard(mode="allocation").evaluate(open_order(600), 0, None)
    assert allowed is False
    assert "exceeds max" in reason


def test_full_capital_allows_notional_21000():
    allowed, reason = guard(mode="full_capital").evaluate(open_order(21000), 0, None)
    assert (allowed, reason) == (True, "ok")


# --- everything else still applies in full_capital ----------------------- #

def test_full_capital_still_rejects_below_min_notional():
    allowed, reason = guard(mode="full_capital").evaluate(open_order(10), 0, None)
    assert allowed is False
    assert reason == "below min notional"


def test_full_capital_still_rejects_leverage_above_max():
    g = guard(max_leverage=50, mode="full_capital")
    allowed, reason = g.evaluate(open_order(21000, leverage=75), 0, None)
    assert allowed is False
    assert "exceeds exchange max" in reason


def test_full_capital_still_rejects_when_position_open():
    allowed, reason = guard(mode="full_capital").evaluate(open_order(21000), 0.5, None)
    assert allowed is False
    assert reason == "position already open"


def test_full_capital_still_rejects_inside_min_interval():
    g = guard(mode="full_capital")
    allowed, reason = g.evaluate(open_order(21000), 0, time.time() - 5)
    assert allowed is False
    assert reason == "min order interval not elapsed"


def test_full_capital_allows_open_after_min_interval():
    g = guard(mode="full_capital")
    stale = time.time() - (RiskGuard.MIN_ORDER_INTERVAL_SECONDS + 1)
    assert g.evaluate(open_order(21000), 0, stale) == (True, "ok")


# --- CLOSE is never blocked --------------------------------------------- #

def test_close_allowed_in_full_capital_with_matching_position():
    g = guard(mode="full_capital")
    assert g.evaluate(close_order("LONG"), 1.5, time.time()) == (True, "ok")


def test_close_short_allowed_in_full_capital_with_matching_position():
    g = guard(mode="full_capital")
    assert g.evaluate(close_order("SHORT"), -1.5, time.time()) == (True, "ok")


def test_close_rejected_with_no_matching_position():
    allowed, reason = guard(mode="full_capital").evaluate(close_order("LONG"), 0, None)
    assert allowed is False
    assert reason == "no matching position to close"


# --- unrecognised mode never widens ------------------------------------- #

def test_unknown_sizing_mode_behaves_as_allocation_and_logs_error(caplog):
    g = RiskGuard(max_notional_usd=500, max_leverage=90)
    with caplog.at_level(logging.ERROR, logger="executor.risk_guard"):
        g.set_sizing_mode("banana")
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    # Capped behaviour: the 600 notional that allocation rejects is rejected here.
    allowed, reason = g.evaluate(open_order(600), 0, None)
    assert allowed is False
    assert "exceeds max" in reason


def test_constructor_defaults_to_allocation_before_any_refresh():
    g = RiskGuard(max_notional_usd=500, max_leverage=90)
    allowed, _ = g.evaluate(open_order(600), 0, None)
    assert allowed is False


@pytest.mark.parametrize("mode", ["allocation", "full_capital"])
def test_symbol_allowlist_unchanged_in_both_modes(mode):
    g = guard(mode=mode)
    order = open_order(100)
    order["symbol"] = "BTCUSDT"
    allowed, reason = g.evaluate(order, 0, None)
    assert allowed is False
    assert "not allowed" in reason


def test_absolute_max_notional_constant_still_present():
    assert RiskGuard.ABSOLUTE_MAX_NOTIONAL_USD == 500
