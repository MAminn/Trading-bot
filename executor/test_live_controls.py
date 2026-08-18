"""Phase 3: the environment is a ceiling the database can never lift.

Pure logic, no network, no credentials. Every test here asserts either a
refusal or a narrowing — there is no test that grants capability, because
nothing in this module is allowed to.
"""

from decimal import Decimal

import pytest

import main
from live_controls import (
    DB_MODE_LEVEL,
    ENV_MODE_LEVEL,
    TRADE_CAPABLE_MODES,
    allow_full_capital,
    db_mode_level,
    is_trade_capable,
    placement_block_reason,
    resolve_effective_mode,
    resolve_live_order_cap,
)
from risk_guard import RiskGuard

ACK = "I_UNDERSTAND_REAL_MONEY"

ENV_MODES = ("OFF", "TESTNET_READ", "TESTNET_TRADE", "LIVE_READ", "LIVE_TRADE")
DB_MODES = ("OFF", "LIVE_READ", "LIVE_TRADE")


def gates(**overrides):
    """The fully-open gate set, so each test can close exactly one thing."""
    kwargs = dict(
        effective_mode="LIVE_TRADE",
        db_execution_mode="LIVE_TRADE",
        auto_execute_enabled=True,
        is_running=True,
        live_order_cap_usd=Decimal("30"),
        ack_present=True,
    )
    kwargs.update(overrides)
    return placement_block_reason(**kwargs)


# --- the module agrees with main.py --------------------------------------- #

def test_trade_capable_set_matches_main():
    assert TRADE_CAPABLE_MODES == main.TRADE_CAPABLE_MODES


def test_every_env_mode_main_allows_has_a_level():
    for mode in main.ALLOWED_MODES:
        assert mode in ENV_MODE_LEVEL


def test_database_cannot_request_a_testnet_mode():
    # The network is a host-local decision; the web has no say in it.
    for mode in DB_MODE_LEVEL:
        assert not mode.startswith("TESTNET")


# --- the lattice ----------------------------------------------------------- #

@pytest.mark.parametrize("db_mode", DB_MODES)
def test_env_off_is_always_off(db_mode):
    assert resolve_effective_mode("OFF", db_mode) == "OFF"


@pytest.mark.parametrize("env_mode", ENV_MODES)
def test_db_off_is_always_off(env_mode):
    assert resolve_effective_mode(env_mode, "OFF") == "OFF"


def test_env_live_read_caps_a_live_trade_request():
    # Requirement 1: a database LIVE_TRADE on a LIVE_READ host degrades.
    assert resolve_effective_mode("LIVE_READ", "LIVE_TRADE") == "LIVE_READ"
    assert not is_trade_capable(resolve_effective_mode("LIVE_READ", "LIVE_TRADE"))


def test_env_live_trade_follows_the_database_downwards():
    assert resolve_effective_mode("LIVE_TRADE", "OFF") == "OFF"
    assert resolve_effective_mode("LIVE_TRADE", "LIVE_READ") == "LIVE_READ"
    assert resolve_effective_mode("LIVE_TRADE", "LIVE_TRADE") == "LIVE_TRADE"


def test_network_comes_from_env_only():
    # A mainnet request on a testnet host stays on testnet. This is the gap
    # that would otherwise turn a staging box into a real-money one.
    assert resolve_effective_mode("TESTNET_TRADE", "LIVE_TRADE") == "TESTNET_TRADE"
    assert resolve_effective_mode("TESTNET_READ", "LIVE_TRADE") == "TESTNET_READ"
    assert resolve_effective_mode("TESTNET_TRADE", "LIVE_READ") == "TESTNET_READ"


@pytest.mark.parametrize(
    "bad", [None, "", "banana", "live_trade", "LIVE", 1, True, object()]
)
def test_unrecognised_db_request_fails_closed(bad):
    assert db_mode_level(bad) == 0
    assert resolve_effective_mode("LIVE_TRADE", bad) == "OFF"


@pytest.mark.parametrize("bad", [None, "", "banana", "DB_LIVE_TRADE"])
def test_unrecognised_env_mode_fails_closed(bad):
    assert resolve_effective_mode(bad, "LIVE_TRADE") == "OFF"


def test_effective_mode_never_exceeds_env_capability():
    # Exhaustive: no (env, db) pair may produce more capability than env alone.
    for env_mode in ENV_MODES:
        for db_mode in DB_MODES:
            effective = resolve_effective_mode(env_mode, db_mode)
            assert ENV_MODE_LEVEL[effective] <= ENV_MODE_LEVEL[env_mode]


def test_only_env_trade_modes_can_ever_be_trade_capable():
    for env_mode in ENV_MODES:
        for db_mode in DB_MODES:
            if is_trade_capable(resolve_effective_mode(env_mode, db_mode)):
                assert env_mode in TRADE_CAPABLE_MODES


# --- the placement gate: the required behaviour matrix --------------------- #

def test_env_live_read_plus_db_live_trade_blocks_placement():
    effective = resolve_effective_mode("LIVE_READ", "LIVE_TRADE")
    assert gates(effective_mode=effective) == "effective_mode=LIVE_READ"


def test_env_live_trade_plus_db_off_blocks_placement():
    effective = resolve_effective_mode("LIVE_TRADE", "OFF")
    assert gates(effective_mode=effective, db_execution_mode="OFF") == "effective_mode=OFF"


def test_env_live_trade_plus_db_live_read_blocks_placement():
    effective = resolve_effective_mode("LIVE_TRADE", "LIVE_READ")
    assert (
        gates(effective_mode=effective, db_execution_mode="LIVE_READ")
        == "effective_mode=LIVE_READ"
    )


def test_signal_only_blocks_placement():
    assert gates(auto_execute_enabled=False) == "auto_execute_disabled"


def test_kill_switch_blocks_placement():
    assert gates(is_running=False) == "kill_switch_active"


def test_all_gates_open_allows_placement():
    # The one positive case in this file: every gate open, nothing blocking.
    assert gates() is None


def test_missing_ack_blocks_placement_even_with_everything_else_open():
    assert gates(ack_present=False) == "live_trading_ack_missing"


@pytest.mark.parametrize("cap", [None, 0, Decimal("0"), -1, "banana"])
def test_invalid_cap_blocks_placement(cap):
    assert gates(live_order_cap_usd=cap).startswith("live_order_cap_invalid")


def test_db_mode_is_checked_independently_of_effective_mode():
    # Redundant by construction, and deliberately not removed: if a future edit
    # broke the lattice, this second check still refuses.
    assert gates(db_execution_mode="LIVE_READ") == "db_execution_mode='LIVE_READ'"


@pytest.mark.parametrize("truthy", [1, "true", "yes", [1]])
def test_auto_execute_requires_exactly_true(truthy):
    # A truthy value that is not True must not open the gate.
    assert gates(auto_execute_enabled=truthy) == "auto_execute_disabled"


@pytest.mark.parametrize("truthy", [1, "true", "yes"])
def test_is_running_requires_exactly_true(truthy):
    assert gates(is_running=truthy) == "kill_switch_active"


# --- the effective cap ----------------------------------------------------- #

def test_effective_cap_is_the_minimum_of_host_and_database():
    assert resolve_live_order_cap(30, 500) == Decimal("30")
    assert resolve_live_order_cap(500, 30) == Decimal("30")
    assert resolve_live_order_cap(30, 30) == Decimal("30")


def test_database_can_never_raise_the_cap():
    for db_cap in (0, 1, 30, 100, 499, 500, 10_000, 10**9):
        assert resolve_live_order_cap(30, db_cap) <= Decimal("30")


def test_absent_database_cap_leaves_the_host_ceiling():
    assert resolve_live_order_cap(30, None) == Decimal("30")


def test_unreadable_database_cap_does_not_widen():
    for bad in ("banana", "", object()):
        assert resolve_live_order_cap(30, bad) == Decimal("30")


def test_no_cap_anywhere_is_none_rather_than_unlimited():
    # Only reachable outside LIVE_TRADE, where preflight does not demand one.
    assert resolve_live_order_cap(None, None) is None


def test_database_cap_alone_applies_when_host_has_none():
    assert resolve_live_order_cap(None, 30) == Decimal("30")


# --- the risk guard honours the ceiling ------------------------------------ #

def test_risk_guard_cap_can_be_lowered_but_never_raised():
    guard = RiskGuard(max_notional_usd=500, max_leverage=90, live_cap_usd=30)
    guard.set_live_cap(10)
    assert guard._live_cap_usd == 10
    guard.set_live_cap(500)
    assert guard._live_cap_usd == 30  # clamped back to the host ceiling
    guard.set_live_cap(None)
    assert guard._live_cap_usd == 30


def test_risk_guard_rejects_an_order_above_the_effective_cap():
    guard = RiskGuard(max_notional_usd=500, max_leverage=90, live_cap_usd=30)
    guard.set_live_cap(25)
    order = {
        "symbol": "ETHUSDT",
        "intent": "OPEN",
        "qty": 0.02,
        "notional_usd": 28,
        "leverage": 1,
    }
    allowed, reason = guard.evaluate(order, 0, None)
    assert allowed is False
    assert "live cap" in reason


def test_risk_guard_unreadable_cap_falls_back_to_ceiling():
    guard = RiskGuard(max_notional_usd=500, max_leverage=90, live_cap_usd=30)
    guard.set_live_cap("banana")
    assert guard._live_cap_usd == 30


# --- an idle cycle must not strand the refresh flag ------------------------ #

def test_end_cycle_lets_the_next_refresh_happen():
    """An OFF cycle returns before poll_once, which is what normally reopens
    the cycle. Without end_cycle() the executor would refresh once, stay OFF,
    and never see the database change back."""
    from signal_consumer import SignalConsumer

    c = SignalConsumer("http://app.invalid", "tok", "user", "LIVE_READ", "ETHUSDT")
    refreshes = {"n": 0}

    def fake_get(path, params):
        refreshes["n"] += 1
        return {
            "config": {
                "leverage": 1,
                "capital_allocation_pct": 10,
                "capital_usd": 100,
                "execution_mode": "OFF",
            }
        }

    c._get = fake_get

    c.ensure_config()
    assert refreshes["n"] == 1
    c.ensure_config()  # same cycle: must not refresh again
    assert refreshes["n"] == 1

    c.end_cycle()
    c.ensure_config()  # next cycle: must refresh
    assert refreshes["n"] == 2


def test_config_is_refreshed_every_cycle():
    """The control plane cannot lag: a stop must take effect next cycle."""
    from signal_consumer import CONFIG_REFRESH_EVERY_CYCLES

    assert CONFIG_REFRESH_EVERY_CYCLES == 1


# --- full capital needs both consents -------------------------------------- #

@pytest.mark.parametrize(
    "env_allow,db_allow,expected",
    [
        (False, False, False),
        (False, True, False),  # database alone is not enough
        (True, False, False),  # host alone is not enough
        (True, True, True),
    ],
)
def test_full_capital_requires_both_consents(env_allow, db_allow, expected):
    assert allow_full_capital(env_allow, db_allow) is expected


@pytest.mark.parametrize("db_allow", [None, "true", 1, "1", [], object()])
def test_full_capital_database_consent_must_be_exactly_true(db_allow):
    assert allow_full_capital(True, db_allow) is False
