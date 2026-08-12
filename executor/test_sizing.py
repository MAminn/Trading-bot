"""Gate 2 + Gate 4: pure-logic sizing tests for SignalConsumer.

No network: the consumer is constructed and its config state is injected
directly, then _build_intent is exercised. Nothing here places or posts orders.
"""

import logging
from decimal import ROUND_DOWN, Decimal

import pytest

from signal_consumer import (
    HARD_CAP_USD,
    MARGIN_SAFETY_FRACTION,
    MIN_NOTIONAL_USD,
    STEP_SIZE,
    SignalConsumer,
)

USER = "11111111-1111-1111-1111-111111111111"
SIGNAL = {"rule_side": 1, "bar_time": "2026-08-13T00:00:00Z"}

# A representative ETHUSDT ladder: tier 0 is the highest leverage and carries
# the SMALLEST notionalCap, which is precisely the trap Step 2 removes.
LADDER = [
    {"initialLeverage": 90, "notionalCap": Decimal("50000")},
    {"initialLeverage": 50, "notionalCap": Decimal("250000")},
    {"initialLeverage": 25, "notionalCap": Decimal("1000000")},
    {"initialLeverage": 10, "notionalCap": Decimal("5000000")},
    {"initialLeverage": 5, "notionalCap": Decimal("20000000")},
]


def make_consumer(
    *,
    mode="allocation",
    capital_usd="100",
    account_size_usd=None,
    leverage="30",
    max_notional_usd="500",
    bracket_cap=None,
    available_balance="1000",
    config_invalid=False,
):
    c = SignalConsumer("http://app.invalid", "token", USER, "TESTNET_READ", "ETHUSDT")
    c._sizing_mode = mode
    c._capital_usd = Decimal(capital_usd)
    c._account_size_usd = (
        None if account_size_usd is None else Decimal(account_size_usd)
    )
    c._leverage = Decimal(leverage)
    c._max_notional_usd = Decimal(max_notional_usd)
    c._bracket_notional_cap = (
        None if bracket_cap is None else Decimal(bracket_cap)
    )
    c._available_balance = (
        None if available_balance is None else Decimal(available_balance)
    )
    c._config_invalid = config_invalid
    return c


# ---------------------------------------------------------------------- #
# Gate 2: bracket selection by configured leverage
# ---------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "configured,expected_lev,expected_cap",
    [
        (90, 90, Decimal("50000")),
        (50, 50, Decimal("250000")),
        (30, 50, Decimal("250000")),
        (25, 25, Decimal("1000000")),
        (10, 10, Decimal("5000000")),
        (5, 5, Decimal("20000000")),
        (1, 5, Decimal("20000000")),
    ],
)
def test_select_bracket_picks_largest_qualifying_cap(
    configured, expected_lev, expected_cap
):
    selected = SignalConsumer.select_bracket(LADDER, configured)
    assert selected is not None
    assert selected["initialLeverage"] == expected_lev
    assert selected["notionalCap"] == expected_cap


def test_select_bracket_returns_none_when_no_tier_qualifies():
    assert SignalConsumer.select_bracket(LADDER, 125) is None


def test_lower_leverage_yields_larger_cap_than_30x():
    cap_30 = SignalConsumer.select_bracket(LADDER, 30)["notionalCap"]
    cap_5 = SignalConsumer.select_bracket(LADDER, 5)["notionalCap"]
    assert cap_5 > cap_30


def test_set_leverage_limits_selects_for_configured_leverage(caplog):
    c = make_consumer(leverage="5")
    with caplog.at_level(logging.INFO, logger="executor.consumer"):
        c.set_leverage_limits(90, LADDER)
    assert c._bracket_notional_cap == Decimal("20000000")
    assert c._exchange_max_leverage == 90
    assert "BRACKET SELECTED" in caplog.text


def test_unselectable_bracket_fails_closed(caplog):
    c = make_consumer(leverage="125")
    with caplog.at_level(logging.ERROR, logger="executor.consumer"):
        c.set_leverage_limits(90, LADDER)
    assert c._bracket_notional_cap is None
    assert c._config_invalid is True


def test_no_ladder_leaves_cap_unset_without_error():
    """Read-only modes never probe; that must stay a non-error, as before."""
    c = make_consumer()
    c._apply_bracket_selection()
    assert c._bracket_notional_cap is None
    assert c._config_invalid is False


# ---------------------------------------------------------------------- #
# Gate 4.1: allocation-mode regression — bit-identical to pre-change
# ---------------------------------------------------------------------- #

def legacy_build(capital_usd, leverage, max_notional, bracket_cap, ref_price, balance):
    """The sizing block exactly as it was before this change."""
    target = capital_usd * leverage
    caps = [target, max_notional, HARD_CAP_USD]
    if bracket_cap is not None:
        caps.append(bracket_cap)
    notional_target = min(caps)
    qty = (notional_target / ref_price).quantize(STEP_SIZE, rounding=ROUND_DOWN)
    notional = qty * ref_price
    status, error = "INTENT_LOGGED", None
    if notional < MIN_NOTIONAL_USD:
        status, error = "SKIPPED", "below min notional"
    elif balance is not None and (
        notional / leverage > balance * MARGIN_SAFETY_FRACTION
    ):
        status, error = "SKIPPED", "insufficient margin for configured size"
    return float(qty), float(notional), status, error


ALLOCATION_MATRIX = [
    (cap, lev, maxn, brk, price, bal)
    for cap in ("10", "100", "250", "1000")
    for lev in ("1", "10", "30", "90")
    for maxn in ("200", "500", "100000")
    for brk in (None, "50000", "300")
    for price in ("2500.55", "4000")
    for bal in (None, "50", "1000", "100000")
]


@pytest.mark.parametrize("cap,lev,maxn,brk,price,bal", ALLOCATION_MATRIX)
def test_4_1_allocation_notional_bit_identical(cap, lev, maxn, brk, price, bal):
    c = make_consumer(
        mode="allocation",
        capital_usd=cap,
        leverage=lev,
        max_notional_usd=maxn,
        bracket_cap=brk,
        available_balance=bal,
    )
    ref = Decimal(price)
    order = c._build_intent(SIGNAL, ref)
    expected = legacy_build(
        Decimal(cap),
        Decimal(lev),
        Decimal(maxn),
        None if brk is None else Decimal(brk),
        ref,
        None if bal is None else Decimal(bal),
    )
    assert (
        order["qty"],
        order["notional_usd"],
        order["status"],
        order.get("error"),
    ) == expected


# ---------------------------------------------------------------------- #
# Gate 4.2 - 4.7: full_capital behaviour
# ---------------------------------------------------------------------- #

def test_4_2_full_capital_target_is_base_times_leverage_no_hard_cap(caplog):
    # account_size 300 x 5 = 1500 target, far above HARD_CAP_USD (500).
    c = make_consumer(
        mode="full_capital",
        account_size_usd="300",
        leverage="5",
        available_balance="100000",
    )
    with caplog.at_level(logging.INFO, logger="executor.consumer"):
        order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["status"] == "INTENT_LOGGED"
    assert order["notional_usd"] == pytest.approx(1500.0)
    assert "hard_cap" not in caplog.text
    assert float(HARD_CAP_USD) < order["notional_usd"]


def test_4_3_account_size_above_wallet_clamps_to_available_margin(caplog):
    c = make_consumer(
        mode="full_capital",
        account_size_usd="1000000",
        leverage="10",
        available_balance="500",
    )
    with caplog.at_level(logging.INFO, logger="executor.consumer"):
        order = c._build_intent(SIGNAL, Decimal("2500"))
    # base = 500 * 0.90 = 450 -> target 4500
    assert order["status"] == "INTENT_LOGGED", order.get("error")
    assert order["notional_usd"] == pytest.approx(4500.0)
    assert "bound_by=available_margin" in caplog.text


def test_4_4_unknown_balance_blocks_open_in_full_capital():
    c = make_consumer(
        mode="full_capital", account_size_usd="300", available_balance=None
    )
    order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["status"] == "SKIPPED"
    assert order["error"] == "available balance unknown"
    assert order["qty"] == 0.0
    assert order["notional_usd"] == 0.0


def test_allocation_tolerates_unknown_balance_because_hard_cap_binds():
    c = make_consumer(mode="allocation", available_balance=None)
    order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["status"] == "INTENT_LOGGED"
    assert order["notional_usd"] <= float(HARD_CAP_USD)


def test_4_5_invalid_config_blocks_open_but_close_still_builds(caplog):
    c = make_consumer(config_invalid=True)
    with caplog.at_level(logging.ERROR, logger="executor.consumer"):
        order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["status"] == "SKIPPED"
    assert order["error"] == "invalid sizing config"
    assert order["qty"] == 0.0
    assert any(r.levelno == logging.ERROR for r in caplog.records)

    # A CLOSE is never gated on sizing config.
    close_signal = {
        "bar_time": "2026-08-13T00:00:00Z",
        "position_before": "LONG",
        "closed_reason": "flat",
        "position_after": "FLAT",
    }
    close = c._build_close_intent(close_signal, Decimal("2500"), 1.234)
    assert close is not None
    assert close["status"] == "INTENT_LOGGED"
    assert close["intent"] == "CLOSE"
    assert close["qty"] == 1.234


def test_4_6_full_capital_below_min_notional_is_skipped():
    c = make_consumer(
        mode="full_capital",
        account_size_usd="1",
        leverage="1",
        available_balance="100000",
    )
    order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["status"] == "SKIPPED"
    assert order["error"] == "below min notional"


def test_4_7_bracket_cap_uses_selected_tier_not_tier_zero(caplog):
    c = make_consumer(
        mode="full_capital",
        account_size_usd="100000",
        leverage="5",
        available_balance="10000000",
    )
    c.set_leverage_limits(90, LADDER)
    with caplog.at_level(logging.INFO, logger="executor.consumer"):
        order = c._build_intent(SIGNAL, Decimal("2500"))
    # Selected tier for 5x caps at 20,000,000 — not tier 0's 50,000.
    assert c._bracket_notional_cap == Decimal("20000000")
    # target = 100000 * 5 = 500000, under the selected cap, so no bracket clamp.
    assert order["notional_usd"] == pytest.approx(500000.0)
    assert "bound_by=bracket_cap" not in caplog.text
    # Under tier 0's cap it would have been clamped to 50,000.
    assert order["notional_usd"] > 50000.0


def test_full_capital_bound_by_bracket_cap_when_target_exceeds_it(caplog):
    c = make_consumer(
        mode="full_capital",
        account_size_usd="100000",
        leverage="90",
        available_balance="10000000",
    )
    c.set_leverage_limits(90, LADDER)
    with caplog.at_level(logging.INFO, logger="executor.consumer"):
        order = c._build_intent(SIGNAL, Decimal("2500"))
    assert "bound_by=bracket_cap" in caplog.text
    assert order["notional_usd"] == pytest.approx(50000.0)


def test_full_capital_never_consults_max_notional_usd():
    """max_notional_usd is deliberately tiny; it must not bind in full_capital."""
    c = make_consumer(
        mode="full_capital",
        account_size_usd="300",
        leverage="5",
        max_notional_usd="25",
        available_balance="100000",
    )
    order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["notional_usd"] == pytest.approx(1500.0)


def test_margin_sufficiency_check_retained_in_both_modes():
    """Second line of defence: still present on the full_capital path."""
    import inspect

    src = inspect.getsource(SignalConsumer._build_intent)
    assert "insufficient margin for configured size" in src
