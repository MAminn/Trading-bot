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
    db_mode_level,
    is_trade_capable,
    placement_block_reason,
    resolve_effective_mode,
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


# --- no per-order dollar cap exists anywhere ------------------------------- #

def test_no_cap_resolution_helper_remains():
    """resolve_live_order_cap() combined a host env ceiling with a database
    request to produce a per-order dollar limit. Order size is now the client's
    own wallet x allocation x leverage, so a helper whose only purpose is to
    produce a competing number must not exist."""
    import inspect

    import live_controls

    assert not hasattr(live_controls, "resolve_live_order_cap")
    src = inspect.getsource(live_controls)
    assert "live_order_cap" not in src
    assert "LIVE_ORDER_CAP" not in src


def test_placement_gates_take_no_cap_argument():
    """Every remaining gate is binary. None of them can change order SIZE — a
    control that resizes rather than halts is a sizing model in disguise."""
    import inspect

    params = set(inspect.signature(placement_block_reason).parameters)
    assert params == {
        "effective_mode",
        "db_execution_mode",
        "auto_execute_enabled",
        "is_running",
        "ack_present",
    }


def test_placement_is_allowed_with_no_cap_configured_anywhere():
    """The old gate refused to place unless a positive cap existed. With the cap
    gone, an otherwise fully-open host must place."""
    assert gates() is None


def test_risk_guard_has_no_cap_api():
    import inspect

    import risk_guard as risk_guard_module

    assert not hasattr(RiskGuard, "set_live_cap")
    src = inspect.getsource(risk_guard_module)
    assert "live_cap" not in src
    assert "notional >" not in src  # no upper notional test survives


def test_main_has_no_cap_constant_or_telemetry_reader():
    assert not hasattr(main, "LIVE_ORDER_CAP_MAX_USD")
    assert not hasattr(main, "LIVE_ORDER_CAP_ENV")
    assert not hasattr(main, "read_env_order_cap_for_telemetry")
    # The name survives only so a host that still sets it is warned.
    assert main.RETIRED_ORDER_CAP_ENV == "LIVE_ORDER_CAP_USD"


def test_live_trade_starts_without_a_cap(monkeypatch):
    """The cap used to be mandatory in LIVE_TRADE. It no longer exists, so its
    absence must not refuse the start."""
    monkeypatch.delenv("LIVE_ORDER_CAP_USD", raising=False)
    monkeypatch.setenv("LIVE_TRADING_ACK", ACK)
    assert main.live_preflight("LIVE_TRADE") is None


def test_a_stale_cap_variable_is_ignored_but_announced(monkeypatch, caplog):
    """An operator with the old value still in .env must be told it does
    nothing, rather than silently believing orders are bounded by it."""
    import logging

    monkeypatch.setenv("LIVE_TRADING_ACK", ACK)
    monkeypatch.setenv("LIVE_ORDER_CAP_USD", "500")
    with caplog.at_level(logging.WARNING, logger="executor"):
        assert main.live_preflight("LIVE_TRADE") is None
    assert "IGNORING LIVE_ORDER_CAP_USD" in caplog.text


def test_ack_is_still_required(monkeypatch):
    """Removing the cap must not weaken the control that actually gates real
    money."""
    monkeypatch.delenv("LIVE_TRADING_ACK", raising=False)
    assert main.live_preflight("LIVE_TRADE") == 1


# --- the idle heartbeat must carry the control state, not nulls ------------ #

def idle_snapshot():
    """The snapshot the effective-OFF idle path builds, as main.py builds it."""
    from executor_status import build_snapshot

    effective = resolve_effective_mode("LIVE_READ", "OFF")
    reason = placement_block_reason(
        effective_mode=effective,
        db_execution_mode="OFF",
        auto_execute_enabled=False,
        is_running=False,
        ack_present=False,
    )
    return build_snapshot(
        mode=effective,
        env_mode_ceiling="LIVE_READ",
        db_execution_mode="OFF",
        auto_execute_enabled=False,
        orders_enabled=reason is None,
        blocked_reason=reason,
        account=None,
        positions=None,
        reconcile=None,
        keys_present=True,
        permission_status=None,
        message="idle: effective OFF (env LIVE_READ, database OFF)",
    )


def test_idle_telemetry_reports_the_control_state_rather_than_nulls():
    """env LIVE_READ + database OFF: the heartbeat must say WHY it is idle.

    The regression this pins: these fields arrived null in production because
    the ingest route's schema omitted them, so an idle executor looked
    indistinguishable from one that had never reported a control state."""
    s = idle_snapshot()
    assert s["effective_mode"] == "OFF"
    assert s["env_mode_ceiling"] == "LIVE_READ"
    assert s["db_execution_mode"] == "OFF"
    assert s["auto_execute_enabled"] is False
    assert s["orders_enabled"] is False
    assert s["blocked_reason"] == "effective_mode=OFF"
    for field in (
        "db_execution_mode",
        "auto_execute_enabled",
        "orders_enabled",
        "blocked_reason",
    ):
        assert s[field] is not None, field


def test_telemetry_no_longer_reports_a_cap():
    """A dashboard tile reading "Effective order cap: $500" would now be false."""
    s = idle_snapshot()
    assert "live_order_cap_usd" not in s
    assert "live_order_cap_env_max" not in s


# --- the full-capital consent path is gone entirely ------------------------ #

def test_no_full_capital_consent_helper_remains():
    """allow_full_capital() required a host env flag AND a database column, and
    was the gate on the second sizing path. Both halves are removed, so the
    helper must not exist for anything to call."""
    import inspect

    import live_controls

    assert not hasattr(live_controls, "allow_full_capital")
    src = inspect.getsource(live_controls)
    assert "full_capital" not in src
    assert "FULL_CAPITAL" not in src
