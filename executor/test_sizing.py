"""Sizing tests for SignalConsumer — the single sizing model.

    target notional = totalWalletBalance x allocation% x leverage

No network: the consumer is constructed and its state is injected directly,
then _build_intent is exercised. Nothing here places or posts orders.

What these tests exist to hold down:
  * the formula, exactly, at the worked examples;
  * that allocation and leverage are INDEPENDENT — moving one changes nothing
    about the other;
  * that totalWalletBalance, not availableBalance, is the capital base;
  * that each session sizes against its own user's wallet and no other's;
  * that only exchange constraints and the operator's live cap may reduce the
    result, and that each names itself when it binds;
  * that no full-capital path, sizing mode, account-size field or internal
    $500 ceiling survives anywhere in the module.
"""

import inspect
import logging
from decimal import ROUND_DOWN, Decimal

import pytest

import signal_consumer
from signal_consumer import MIN_NOTIONAL_USD, STEP_SIZE, SignalConsumer

USER = "11111111-1111-1111-1111-111111111111"
SIGNAL = {"rule_side": 1, "bar_time": "2026-08-13T00:00:00Z"}

# A representative ETHUSDT ladder: tier 0 is the highest leverage and carries
# the SMALLEST notionalCap, which is precisely the trap bracket selection avoids.
LADDER = [
    {"initialLeverage": 90, "notionalCap": Decimal("50000")},
    {"initialLeverage": 50, "notionalCap": Decimal("250000")},
    {"initialLeverage": 25, "notionalCap": Decimal("1000000")},
    {"initialLeverage": 10, "notionalCap": Decimal("5000000")},
    {"initialLeverage": 5, "notionalCap": Decimal("20000000")},
]


def make_consumer(
    *,
    wallet_balance="300",
    alloc_pct="20",
    leverage="30",
    bracket_cap=None,
    available_balance=None,
    config_invalid=False,
    user_id=USER,
):
    """A consumer with sizing state injected.

    `available_balance` defaults to the wallet balance: a flat account with no
    margin already posted, which is the normal case and keeps the sanity check
    out of the way of tests that are about the formula.
    """
    c = SignalConsumer("http://app.invalid", "token", user_id, "TESTNET_READ", "ETHUSDT")
    c.set_account_balances(
        wallet_balance=wallet_balance,
        available_balance=wallet_balance if available_balance is None else available_balance,
    )
    c._alloc_pct = Decimal(alloc_pct)
    c._leverage = Decimal(leverage)
    c._bracket_notional_cap = None if bracket_cap is None else Decimal(bracket_cap)
    c._config_invalid = config_invalid
    return c


# ---------------------------------------------------------------------- #
# The formula, at the worked examples
# ---------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "wallet,alloc,lev,expected_margin,expected_notional",
    [
        # The three examples named in the specification.
        ("300", "10", "1", 30.0, 30.0),
        ("300", "20", "30", 60.0, 1800.0),
        ("300", "100", "10", 300.0, 3000.0),
        # Every allocation step at a fixed leverage.
        ("1000", "1", "10", 10.0, 100.0),
        ("1000", "5", "10", 50.0, 500.0),
        ("1000", "50", "10", 500.0, 5000.0),
        ("1000", "90", "10", 900.0, 9000.0),
        # Every leverage step at a fixed allocation.
        ("1000", "10", "1", 100.0, 100.0),
        ("1000", "10", "40", 100.0, 4000.0),
        ("1000", "10", "90", 100.0, 9000.0),
    ],
)
def test_formula_is_wallet_times_allocation_times_leverage(
    wallet, alloc, lev, expected_margin, expected_notional
):
    c = make_consumer(wallet_balance=wallet, alloc_pct=alloc, leverage=lev)
    assert float(c._allocated_margin()) == pytest.approx(expected_margin)
    assert float(c._target_notional()) == pytest.approx(expected_notional)

    # And the same number reaches the order, up to step-size rounding.
    ref = Decimal("2500")
    order = c._build_intent(SIGNAL, ref)
    assert order["status"] == "INTENT_LOGGED", order.get("error")
    expected_qty = (Decimal(str(expected_notional)) / ref).quantize(
        STEP_SIZE, rounding=ROUND_DOWN
    )
    assert order["qty"] == float(expected_qty)
    assert order["notional_usd"] == pytest.approx(float(expected_qty * ref))


def test_300_wallet_20pct_30x_is_1800_not_a_capped_500():
    """The regression the whole change exists for: no internal ceiling may
    reduce a requested $1,800 to $30 or $500."""
    c = make_consumer(wallet_balance="300", alloc_pct="20", leverage="30")
    order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["notional_usd"] > 1700.0
    assert order["notional_usd"] <= 1800.0


# ---------------------------------------------------------------------- #
# Independence of the two controls
# ---------------------------------------------------------------------- #

@pytest.mark.parametrize("lev", ["1", "10", "30", "60", "90"])
def test_changing_leverage_does_not_alter_allocation(lev):
    """Allocated margin is a function of wallet and allocation alone."""
    c = make_consumer(wallet_balance="300", alloc_pct="20", leverage=lev)
    assert c._allocated_margin() == Decimal("60")
    assert c._alloc_pct == Decimal("20")
    # ... and the notional moves in exact proportion to leverage.
    assert c._target_notional() == Decimal("60") * Decimal(lev)


@pytest.mark.parametrize("alloc", ["1", "10", "20", "50", "100"])
def test_changing_allocation_does_not_alter_leverage(alloc):
    c = make_consumer(wallet_balance="300", alloc_pct=alloc, leverage="30")
    assert c._leverage == Decimal("30")
    assert c._target_notional() == Decimal("300") * Decimal(alloc) / 100 * 30


def test_no_allocation_to_leverage_mapping_exists():
    """The old LEVERAGE_BY_ALLOC table, in any form, would reintroduce the
    coupling these tests forbid."""
    src = inspect.getsource(signal_consumer)
    assert "LEVERAGE_BY_ALLOC" not in src
    assert not hasattr(signal_consumer, "LEVERAGE_BY_ALLOC")


# ---------------------------------------------------------------------- #
# The capital base is totalWalletBalance, from THIS user's account
# ---------------------------------------------------------------------- #

def test_total_wallet_balance_not_available_balance_drives_sizing():
    """availableBalance is far larger here; if it were the base the notional
    would be 10x what the formula says."""
    c = make_consumer(
        wallet_balance="300",
        available_balance="3000",
        alloc_pct="20",
        leverage="30",
    )
    assert c._target_notional() == Decimal("1800")
    order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["notional_usd"] <= 1800.0
    assert order["notional_usd"] > 1700.0


def test_available_balance_never_reduces_the_allocation():
    """A wallet with margin already posted elsewhere still allocates off the
    FULL wallet balance. The only thing availableBalance may do is refuse an
    order whose margin cannot be posted — never quietly shrink one."""
    c = make_consumer(
        wallet_balance="300", available_balance="290", alloc_pct="20", leverage="30"
    )
    # 20% of 300 = 60 margin, and 290 available covers it.
    order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["status"] == "INTENT_LOGGED", order.get("error")
    assert order["notional_usd"] > 1700.0


def test_insufficient_available_margin_skips_rather_than_shrinks():
    c = make_consumer(
        wallet_balance="300", available_balance="10", alloc_pct="100", leverage="10"
    )
    order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["status"] == "SKIPPED"
    assert order["error"] == "insufficient margin for configured size"


def test_full_allocation_at_a_flat_account_is_not_haircut():
    """The old MARGIN_SAFETY_FRACTION took 10% off available balance, which
    made a 100% allocation impossible to express. It is gone."""
    assert not hasattr(signal_consumer, "MARGIN_SAFETY_FRACTION")
    c = make_consumer(wallet_balance="300", alloc_pct="100", leverage="10")
    order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["status"] == "INTENT_LOGGED", order.get("error")
    assert order["notional_usd"] == pytest.approx(3000.0, abs=2.5)


def test_unknown_wallet_balance_blocks_the_open():
    c = make_consumer()
    c.set_account_balances(wallet_balance=None, available_balance="1000")
    order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["status"] == "SKIPPED"
    assert order["error"] == "wallet balance unknown"
    assert order["qty"] == 0.0
    assert order["notional_usd"] == 0.0


def test_unreadable_wallet_balance_blocks_the_open():
    c = make_consumer()
    c.set_account_balances(wallet_balance="not-a-number", available_balance="1000")
    assert c.wallet_balance is None
    order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["status"] == "SKIPPED"
    assert order["error"] == "wallet balance unknown"


def test_each_user_sizes_against_their_own_wallet():
    """Two sessions, two wallets, no shared state. A session holds one user's
    balance on the instance and there is no module-level cache to leak it."""
    alice = make_consumer(
        user_id="aaaaaaaa-1111-1111-1111-111111111111",
        wallet_balance="300",
        alloc_pct="20",
        leverage="30",
    )
    bob = make_consumer(
        user_id="bbbbbbbb-2222-2222-2222-222222222222",
        wallet_balance="10000",
        alloc_pct="20",
        leverage="30",
    )
    assert alice._target_notional() == Decimal("1800")
    assert bob._target_notional() == Decimal("60000")
    # Bob's read does not disturb Alice's.
    bob.set_account_balances(wallet_balance="50000", available_balance="50000")
    assert alice.wallet_balance == Decimal("300")
    assert alice._target_notional() == Decimal("1800")


def test_wallet_balance_is_not_read_from_config():
    """The capital base must come from the exchange. A config key that could
    supply it would be a stored balance by another name, so the refresh must
    read no balance-shaped field and must never assign _wallet_balance."""
    src = inspect.getsource(SignalConsumer._refresh_config)
    for banned in (
        'config.get("account_size_usd")',
        'config.get("capital_usd")',
        'config.get("wallet_balance")',
        'config.get("totalWalletBalance")',
        "self._wallet_balance =",
    ):
        assert banned not in src, f"{banned} appears in _refresh_config"

    # The only writer of the capital base is the exchange reading.
    setter = inspect.getsource(SignalConsumer.set_account_balances)
    assert "self._wallet_balance = self._to_decimal(wallet_balance)" in setter


# ---------------------------------------------------------------------- #
# Step size and minimum notional — exchange constraints
# ---------------------------------------------------------------------- #

@pytest.mark.parametrize("price", ["2500.55", "4000", "1234.56", "3333.33"])
@pytest.mark.parametrize("alloc", ["1", "20", "70", "100"])
def test_quantity_respects_binance_step_size(price, alloc):
    c = make_consumer(wallet_balance="5000", alloc_pct=alloc, leverage="10")
    ref = Decimal(price)
    order = c._build_intent(SIGNAL, ref)
    qty = Decimal(str(order["qty"]))
    # Exactly representable as a whole number of 0.001 steps, and never rounded up.
    assert qty == qty.quantize(STEP_SIZE, rounding=ROUND_DOWN)
    assert (qty / STEP_SIZE) % 1 == 0
    assert qty * ref <= c._target_notional()


def test_below_min_notional_is_skipped():
    c = make_consumer(wallet_balance="10", alloc_pct="1", leverage="1")
    order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["status"] == "SKIPPED"
    assert order["error"] == "below min notional"
    assert MIN_NOTIONAL_USD == Decimal("20")


# ---------------------------------------------------------------------- #
# The caps that remain — and only those
# ---------------------------------------------------------------------- #

def test_no_internal_hard_cap_remains():
    """HARD_CAP_USD and max_notional_usd both defaulted to 500 and would clamp
    a correctly-sized order. Neither may exist."""
    assert not hasattr(signal_consumer, "HARD_CAP_USD")
    src = inspect.getsource(signal_consumer)
    assert "HARD_CAP_USD" not in src
    assert "max_notional_usd" not in src


def test_large_wallet_is_not_clamped_by_any_internal_ceiling():
    c = make_consumer(wallet_balance="100000", alloc_pct="50", leverage="20")
    order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["status"] == "INTENT_LOGGED", order.get("error")
    # 100000 * 50% * 20 = 1,000,000 — nothing internal may reduce this.
    assert order["notional_usd"] > 999_000.0


def test_bracket_cap_binds_and_names_itself(caplog):
    c = make_consumer(wallet_balance="100000", alloc_pct="100", leverage="90")
    c.set_leverage_limits(90, LADDER)
    with caplog.at_level(logging.WARNING, logger="executor.consumer"):
        order = c._build_intent(SIGNAL, Decimal("2500"))
    assert c._bracket_notional_cap == Decimal("50000")
    assert order["notional_usd"] == pytest.approx(50000.0, abs=2.5)
    assert "bound_by=bracket_cap" in caplog.text


def test_the_bracket_cap_is_the_only_cap(caplog):
    """One entry in the cap list, and it is Binance's. An operator dollar cap
    here would silently trade a different size than the client configured."""
    src = inspect.getsource(SignalConsumer._build_intent)
    assert 'caps.append(("bracket_cap"' in src
    assert "live_order_cap" not in src
    assert src.count("caps.append(") == 1


def test_no_cap_state_exists_on_the_consumer():
    c = make_consumer()
    for attr in ("_live_order_cap_usd", "_live_order_cap_ceiling", "live_order_cap_usd"):
        assert not hasattr(c, attr), attr


def test_invalid_config_blocks_open_but_close_still_builds(caplog):
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


def test_close_sizes_from_the_actual_binance_position_only():
    """CLOSE must be unchanged: the quantity is the real position amount, and
    no wallet, allocation or leverage value participates."""
    c = make_consumer(wallet_balance="300", alloc_pct="100", leverage="90")
    close_signal = {
        "bar_time": "2026-08-13T00:00:00Z",
        "position_before": "SHORT",
        "position_after": "FLAT",
    }
    close = c._build_close_intent(close_signal, Decimal("2500"), -0.4567)
    assert close["qty"] == 0.456  # abs, rounded DOWN to step size
    assert close["side"] == "SHORT"
    assert close["intent"] == "CLOSE"

    # Wallet balance is irrelevant to a CLOSE — even an unknown one.
    c.set_account_balances(wallet_balance=None, available_balance=None)
    again = c._build_close_intent(close_signal, Decimal("2500"), -0.4567)
    assert again["qty"] == 0.456
    assert again["status"] == "INTENT_LOGGED"


# ---------------------------------------------------------------------- #
# Bracket selection by configured leverage (unchanged behaviour)
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


def test_set_leverage_limits_selects_for_configured_leverage(caplog):
    c = make_consumer(leverage="1")
    with caplog.at_level(logging.INFO, logger="executor.consumer"):
        c.set_leverage_limits(90, LADDER)
    assert c._bracket_notional_cap == Decimal("20000000")
    assert c._exchange_max_leverage == 90
    assert "BRACKET SELECTED" in caplog.text


def test_leverage_above_exchange_maximum_fails_closed(caplog):
    """No tier permits it, so there is no cap to size under. Blocked, never
    sized without a bound."""
    c = make_consumer(leverage="125")
    with caplog.at_level(logging.ERROR, logger="executor.consumer"):
        c.set_leverage_limits(90, LADDER)
    assert c._bracket_notional_cap is None
    assert c._config_invalid is True
    order = c._build_intent(SIGNAL, Decimal("2500"))
    assert order["status"] == "SKIPPED"
    assert order["error"] == "invalid sizing config"


def test_no_ladder_leaves_cap_unset_without_error():
    """Read-only modes never probe; that must stay a non-error, as before."""
    c = make_consumer()
    c._apply_bracket_selection()
    assert c._bracket_notional_cap is None
    assert c._config_invalid is False


# ---------------------------------------------------------------------- #
# Nothing of the old system survives
# ---------------------------------------------------------------------- #

def test_no_full_capital_path_remains_in_the_module():
    src = inspect.getsource(signal_consumer)
    for banned in (
        "full_capital",
        "FULL_CAPITAL",
        "sizing_mode",
        "SIZING_MODES",
        "account_size",
        "allow_full_capital",
        "MARGIN_SAFETY_FRACTION",
    ):
        assert banned not in src, f"{banned} still present in signal_consumer"
    for banned_attr in ("SIZING_MODES", "LIVE_ALLOW_FULL_CAPITAL_ENV"):
        assert not hasattr(signal_consumer, banned_attr)


def test_margin_sufficiency_check_retained():
    """Second line of defence against an unpostable margin, kept."""
    src = inspect.getsource(SignalConsumer._build_intent)
    assert "insufficient margin for configured size" in src
