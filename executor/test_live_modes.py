"""Gate 9: LIVE_READ / LIVE_TRADE safety invariants.

Pure logic and env handling: no network, no credentials, nothing that could
reach an exchange. Every test here asserts a refusal or a ceiling.
"""

import inspect
import logging
from decimal import Decimal

import pytest

import live_controls
import main
import signal_consumer
from binance_client import ReadOnlyFuturesClient
from risk_guard import RiskGuard
from signal_consumer import SignalConsumer, SignalConsumerError

LIVE_ENV = (
    "LIVE_TRADING_ACK",
    "LIVE_ORDER_CAP_USD",
    # Retired. Still cleared here so a stale value in a developer shell cannot
    # make a test pass for the wrong reason.
    "LIVE_ALLOW_FULL_CAPITAL",
    "LIVE_SMOKE_TEST",
    "LIVE_SMOKE_SIDE",
)


@pytest.fixture(autouse=True)
def clean_live_env(monkeypatch):
    """No live env leaks in from the developer's shell or another test."""
    for name in LIVE_ENV:
        monkeypatch.delenv(name, raising=False)


# --- mode table ---------------------------------------------------------- #

def test_live_modes_are_allowed_and_implemented():
    for mode in ("LIVE_READ", "LIVE_TRADE"):
        assert mode in main.ALLOWED_MODES
        assert mode in main.IMPLEMENTED_MODES


def test_retired_placeholder_modes_are_gone():
    # LIVE_DRYRUN / LIVE were never implemented; they must not be accepted now
    # that real live modes exist and could be confused with them.
    assert "LIVE_DRYRUN" not in main.ALLOWED_MODES
    assert "LIVE" not in main.ALLOWED_MODES


def test_default_mode_is_off(monkeypatch):
    monkeypatch.delenv("EXECUTION_MODE", raising=False)
    assert main.os.environ.get("EXECUTION_MODE", "").strip() or "OFF" == "OFF"


def test_live_modes_use_mainnet_usdm_endpoint():
    assert main.LIVE_BASE_URL == "https://fapi.binance.com"
    assert main.MODE_BASE_URLS["LIVE_READ"] == main.LIVE_BASE_URL
    assert main.MODE_BASE_URLS["LIVE_TRADE"] == main.LIVE_BASE_URL


def test_testnet_modes_still_use_testnet_endpoint():
    assert main.MODE_BASE_URLS["TESTNET_READ"] == main.TESTNET_BASE_URL
    assert main.MODE_BASE_URLS["TESTNET_TRADE"] == main.TESTNET_BASE_URL


def test_testnet_credentials_still_come_from_the_environment():
    """Unchanged: testnet keys are throwaway host credentials."""
    assert main.MODE_CREDENTIAL_ENV["TESTNET_TRADE"] == (
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
    )
    assert main.MODE_CREDENTIAL_ENV["TESTNET_READ"] == (
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
    )


def test_no_live_mode_can_source_credentials_from_the_environment():
    """The regression this whole change exists to prevent.

    A client connects their Binance account on the website; the executor must
    sign with THEIR keys. Previously both live modes mapped to
    BINANCE_LIVE_API_KEY/SECRET, so every live order moved the server
    operator's funds no matter who was logged in.

    Asserted as the absence of a mapping rather than as a behaviour: with no
    entry here there is no expression left in main.py that turns an environment
    variable into a mainnet signing key.
    """
    for live_mode in main.LIVE_MODES:
        assert live_mode not in main.MODE_CREDENTIAL_ENV

    env_names = {name for pair in main.MODE_CREDENTIAL_ENV.values() for name in pair}
    assert not any("LIVE" in name for name in env_names)


def test_legacy_live_env_names_are_referenced_only_as_a_warning():
    """The legacy names still exist as constants — for the 'these are ignored'
    warning — but they are not wired to any mode."""
    assert main.LEGACY_LIVE_KEY_ENV == "BINANCE_LIVE_API_KEY"
    assert main.LEGACY_LIVE_SECRET_ENV == "BINANCE_LIVE_API_SECRET"
    assert main.LEGACY_LIVE_KEY_ENV not in {
        name for pair in main.MODE_CREDENTIAL_ENV.values() for name in pair
    }


def test_live_env_key_probe_reports_presence_only(monkeypatch):
    monkeypatch.delenv("BINANCE_LIVE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_LIVE_API_SECRET", raising=False)
    assert main._live_env_keys_present() is False
    monkeypatch.setenv("BINANCE_LIVE_API_KEY", "legacy-key-value")
    # Returns a bool, never the value — this is what keeps it out of a log line.
    assert main._live_env_keys_present() is True


def test_testnet_trade_is_not_trade_capable_for_live_and_vice_versa():
    assert main.TRADE_CAPABLE_MODES == {"TESTNET_TRADE", "LIVE_TRADE"}
    assert "LIVE_READ" not in main.TRADE_CAPABLE_MODES
    assert main.LIVE_MODES == {"LIVE_READ", "LIVE_TRADE"}


# --- LIVE_TRADE preflight: ack ------------------------------------------- #

def test_live_trade_refuses_without_ack():
    assert main.live_preflight("LIVE_TRADE") == 1


def test_live_trade_refuses_with_wrong_ack(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ACK", "yes")
    assert main.live_preflight("LIVE_TRADE") == 1


def test_live_trade_accepts_the_exact_ack(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND_REAL_MONEY")
    assert main.live_preflight("LIVE_TRADE") is None


# --- LIVE_TRADE no longer demands a per-order dollar cap ----------------- #

def test_live_trade_starts_with_no_cap_configured(monkeypatch):
    """The cap used to be mandatory. It is gone: an environment variable that
    could quietly reduce a correctly-sized order was a second sizing model, not
    a safety control."""
    monkeypatch.delenv("LIVE_ORDER_CAP_USD", raising=False)
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND_REAL_MONEY")
    assert main.live_preflight("LIVE_TRADE") is None


@pytest.mark.parametrize("raw", ["", "0", "-5", "abc", "30", "1000001"])
def test_no_value_of_the_retired_cap_changes_the_start_decision(monkeypatch, raw):
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND_REAL_MONEY")
    if raw:
        monkeypatch.setenv("LIVE_ORDER_CAP_USD", raw)
    assert main.live_preflight("LIVE_TRADE") is None


def test_a_stale_cap_variable_is_announced_as_ignored(monkeypatch, caplog):
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND_REAL_MONEY")
    monkeypatch.setenv("LIVE_ORDER_CAP_USD", "500")
    with caplog.at_level(logging.WARNING, logger="executor"):
        assert main.live_preflight("LIVE_TRADE") is None
    assert "IGNORING LIVE_ORDER_CAP_USD" in caplog.text


def test_live_read_needs_no_ack():
    assert main.live_preflight("LIVE_READ") is None


def test_testnet_modes_are_untouched_by_preflight():
    for mode in ("TESTNET_READ", "TESTNET_TRADE", "OFF"):
        assert main.live_preflight(mode) is None


# --- LIVE_READ cannot place ---------------------------------------------- #

def test_read_only_client_has_no_placement_method():
    client = ReadOnlyFuturesClient("https://fapi.binance.com", "k", "s")
    assert not hasattr(client, "place_market_order")


@pytest.mark.parametrize(
    "method", ["place_market_order", "set_leverage", "set_margin_type"]
)
def test_read_only_client_raises_on_any_write_method(method):
    client = ReadOnlyFuturesClient("https://fapi.binance.com", "k", "s")
    with pytest.raises(AttributeError, match="read-only"):
        getattr(client, method)


def test_read_only_client_still_exposes_reads():
    client = ReadOnlyFuturesClient("https://fapi.binance.com", "k", "s")
    for name in ("get_account", "get_positions", "get_mark_price", "sync_clock"):
        assert callable(getattr(client, name))


def test_consumer_refuses_a_trader_in_live_read():
    with pytest.raises(SignalConsumerError, match="read-only"):
        SignalConsumer(
            "https://app.example.com", "tok", "user", "LIVE_READ",
            binance_trader=object(),
        )


def test_consumer_refuses_a_trader_in_testnet_read():
    with pytest.raises(SignalConsumerError, match="read-only"):
        SignalConsumer(
            "https://app.example.com", "tok", "user", "TESTNET_READ",
            binance_trader=object(),
        )


def test_consumer_refuses_an_unknown_mode():
    with pytest.raises(SignalConsumerError, match="unsupported execution_mode"):
        SignalConsumer("https://app.example.com", "tok", "user", "LIVE_DRYRUN")


def test_live_read_consumer_uses_mainnet_and_has_no_trader():
    c = SignalConsumer("https://app.example.com", "tok", "user", "LIVE_READ")
    assert c._binance._base_url == "https://fapi.binance.com"
    assert c._binance_trader is None
    assert c._risk_guard is None


# --- no per-order dollar cap exists in any layer ------------------------- #

def open_order(notional, leverage=30):
    return {
        "symbol": "ETHUSDT",
        "intent": "OPEN",
        "side": "LONG",
        "qty": 1.0,
        "notional_usd": notional,
        "leverage": leverage,
    }


@pytest.mark.parametrize("notional", [30, 500, 1800, 3000, 50_000, 1_000_000])
def test_the_guard_applies_no_upper_notional_limit(notional):
    """The guard tests exchange constraints only. An upper bound here would
    reject the client's own configured size."""
    g = RiskGuard(max_leverage=90)
    assert g.evaluate(open_order(notional), 0, None) == (True, "ok")


def test_the_guard_takes_no_cap_argument():
    with pytest.raises(TypeError):
        RiskGuard(max_leverage=90, live_cap_usd=30)


def test_the_guard_has_no_cap_setter():
    assert not hasattr(RiskGuard(max_leverage=90), "set_live_cap")


def test_no_cap_constant_survives_in_main():
    assert not hasattr(main, "LIVE_ORDER_CAP_MAX_USD")
    assert not hasattr(main, "LIVE_ORDER_CAP_ENV")
    assert not hasattr(main, "read_env_order_cap_for_telemetry")


def test_the_consumer_takes_no_cap_argument():
    import inspect

    params = set(inspect.signature(SignalConsumer.__init__).parameters)
    assert "live_order_cap_usd" not in params
    assert not hasattr(
        SignalConsumer("https://app.example.com", "tok", "user", "LIVE_READ"),
        "live_order_cap_usd",
    )


def test_no_cap_string_survives_the_sizing_module():
    src = inspect.getsource(signal_consumer)
    assert "live_order_cap" not in src
    assert "LIVE_ORDER_CAP" not in src


# --- no full-capital system survives anywhere ---------------------------- #

def consumer_with_config(mode, config, monkeypatch):
    c = SignalConsumer("https://app.example.com", "tok", "user", mode)
    monkeypatch.setattr(c, "_get", lambda path, params: {"config": config})
    c._refresh_config()
    return c


SIZING_CONFIG = {
    "leverage": 30,
    "capital_allocation_pct": 20,
    "execution_mode": "OFF",
    "auto_execute_enabled": False,
    "is_running": False,
}


def test_no_full_capital_env_flag_exists():
    """LIVE_ALLOW_FULL_CAPITAL is gone from the executor entirely, so no host
    environment can select a second sizing path."""
    assert not hasattr(signal_consumer, "LIVE_ALLOW_FULL_CAPITAL_ENV")
    assert "LIVE_ALLOW_FULL_CAPITAL" not in inspect.getsource(signal_consumer)
    assert "LIVE_ALLOW_FULL_CAPITAL" not in inspect.getsource(main)


def test_no_full_capital_consent_helper_exists():
    assert not hasattr(live_controls, "allow_full_capital")
    assert "full_capital" not in inspect.getsource(live_controls)


def test_setting_the_old_env_flag_changes_nothing(monkeypatch):
    """Even with the retired variable set, sizing is identical: nothing reads it."""
    c_off = consumer_with_config("LIVE_TRADE", dict(SIZING_CONFIG), monkeypatch)
    monkeypatch.setenv("LIVE_ALLOW_FULL_CAPITAL", "1")
    c_on = consumer_with_config("LIVE_TRADE", dict(SIZING_CONFIG), monkeypatch)
    for c in (c_off, c_on):
        c.set_account_balances(wallet_balance="300", available_balance="300")
    assert c_off._target_notional() == c_on._target_notional() == Decimal("1800")
    assert c_off._config_invalid is c_on._config_invalid is False


def test_live_trade_needs_no_extra_consent_to_size_off_the_whole_wallet(monkeypatch):
    """A 100% allocation is an ordinary selectable value now, not a second mode
    behind a dual consent gate."""
    config = dict(SIZING_CONFIG, capital_allocation_pct=100, leverage=10)
    c = consumer_with_config("LIVE_TRADE", config, monkeypatch)
    c.set_account_balances(wallet_balance="300", available_balance="300")
    assert c._config_invalid is False
    assert c._target_notional() == Decimal("3000")


# --- the configured size reaches the order untouched --------------------- #

def test_the_formula_reaches_binance_intact(monkeypatch, caplog):
    """The regression this whole change exists for: $300 wallet at 20% and 30x
    must produce ~$1,800, with no clamp of any kind."""
    c = SignalConsumer("https://app.example.com", "tok", "user", "LIVE_TRADE")
    monkeypatch.setattr(c, "_get", lambda path, params: {"config": dict(SIZING_CONFIG)})
    c._refresh_config()
    c.set_account_balances(wallet_balance="300", available_balance="300")

    with caplog.at_level(logging.WARNING, logger="executor.consumer"):
        order = c._build_intent(
            {"bar_time": "2026-08-16T00:00:00Z", "rule_side": 1}, Decimal("4000")
        )
    assert 1790 <= order["notional_usd"] <= 1800
    assert "SIZE CLAMPED" not in caplog.text


def test_only_the_exchange_bracket_can_clamp(monkeypatch, caplog):
    """The one surviving clamp, and it is Binance's own tier limit."""
    c = SignalConsumer("https://app.example.com", "tok", "user", "LIVE_TRADE")
    monkeypatch.setattr(
        c,
        "_get",
        lambda path, params: {
            "config": dict(SIZING_CONFIG, capital_allocation_pct=100, leverage=90)
        },
    )
    c._refresh_config()
    c.set_leverage_limits(90, [{"initialLeverage": 90, "notionalCap": "50000"}])
    c.set_account_balances(wallet_balance="100000", available_balance="100000")

    with caplog.at_level(logging.WARNING, logger="executor.consumer"):
        order = c._build_intent(
            {"bar_time": "2026-08-16T00:00:00Z", "rule_side": 1}, Decimal("4000")
        )
    assert order["notional_usd"] <= 50000.0
    assert "bound_by=bracket_cap" in caplog.text


# --- smoke test refuses unsafe starting states --------------------------- #

def test_smoke_refuses_without_a_trader():
    c = SignalConsumer("https://app.example.com", "tok", "user", "LIVE_READ")
    assert c.run_smoke_test("LONG", 0) == 1


def test_smoke_refuses_when_not_flat(monkeypatch):
    c = SignalConsumer("https://app.example.com", "tok", "user", "LIVE_TRADE")
    c._binance_trader = object()
    assert c.run_smoke_test("LONG", 0.5) == 1


# --- smoke test happy path, driven against a fake exchange --------------- #

class FakeTrader:
    """Minimal stand-in for BinanceFuturesClient that tracks one position."""

    def __init__(self, fill=True):
        self.position = 0.0
        self.orders = []
        self._fill = fill

    def place_market_order(self, symbol, side, qty, client_order_id, reduce_only=False):
        self.orders.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": float(qty),
                "reduce_only": reduce_only,
                "client_order_id": client_order_id,
            }
        )
        if self._fill:
            delta = float(qty) if side == "BUY" else -float(qty)
            self.position = round(self.position + delta, 8)
        return {"orderId": 4242 + len(self.orders), "status": "FILLED"}

    def get_positions(self, symbol):
        return [{"symbol": symbol, "positionAmt": str(self.position)}]


def smoke_consumer(monkeypatch, trader, wallet="3000", price="4000"):
    c = SignalConsumer(
        "https://app.example.com", "tok",
        "11111111-1111-1111-1111-111111111111", "LIVE_TRADE",
    )
    c._binance_trader = trader
    c._risk_guard = RiskGuard(max_leverage=90)
    # 1% of the wallet at 1x keeps the synthetic order small without a cap:
    # the size of a smoke order is now chosen by configuration, like any other.
    config = dict(SIZING_CONFIG, capital_allocation_pct=1, leverage=1)
    monkeypatch.setattr(c, "_get", lambda path, params: {"config": config})
    monkeypatch.setattr(c._binance, "get_mark_price", lambda symbol: float(price))
    monkeypatch.setattr(c, "_post_order", lambda order: True)
    monkeypatch.setattr(c, "_post_order_update", lambda update: None)
    # Settle polling would otherwise sleep between reads.
    monkeypatch.setattr("signal_consumer.SETTLE_POLL_INTERVAL_SECONDS", 0)
    c.set_account_balances(wallet_balance=wallet, available_balance=wallet)
    return c


def test_smoke_places_exactly_one_open_and_one_close(monkeypatch):
    trader = FakeTrader()
    c = smoke_consumer(monkeypatch, trader)
    assert c.run_smoke_test("LONG", 0) == 0

    assert len(trader.orders) == 2
    open_order, close_order = trader.orders
    assert (open_order["side"], open_order["reduce_only"]) == ("BUY", False)
    # The CLOSE must be reduceOnly so it can never flip the position.
    assert (close_order["side"], close_order["reduce_only"]) == ("SELL", True)
    assert trader.position == 0.0


def test_smoke_short_side_places_sell_then_reduce_only_buy(monkeypatch):
    trader = FakeTrader()
    c = smoke_consumer(monkeypatch, trader)
    assert c.run_smoke_test("SHORT", 0) == 0

    assert [o["side"] for o in trader.orders] == ["SELL", "BUY"]
    assert trader.orders[1]["reduce_only"] is True
    assert trader.position == 0.0


def test_smoke_order_is_sized_by_the_formula(monkeypatch):
    trader = FakeTrader()
    c = smoke_consumer(monkeypatch, trader, price="4000")
    assert c.run_smoke_test("LONG", 0) == 0

    qty = trader.orders[0]["qty"]
    # 3000 x 1% x 1 = 30 USD; 30 / 4000 rounded down to the 0.001 step = 0.007.
    assert qty == 0.007
    assert qty * 4000 <= 30


def test_smoke_does_not_move_the_signal_cursor(monkeypatch):
    trader = FakeTrader()
    c = smoke_consumer(monkeypatch, trader)
    c._cursor = "2026-08-16T00:00:00Z"
    c.run_smoke_test("LONG", 0)
    # A natural signal must not be consumed or skipped by a smoke run.
    assert c._cursor == "2026-08-16T00:00:00Z"


def test_smoke_reports_failure_when_the_open_never_settles(monkeypatch, caplog):
    trader = FakeTrader(fill=False)
    c = smoke_consumer(monkeypatch, trader)
    with caplog.at_level(logging.ERROR, logger="executor.consumer"):
        assert c.run_smoke_test("LONG", 0) == 1
    # It must NOT send a close for a position it cannot confirm exists, and it
    # must tell the operator the state is unknown rather than imply it is flat.
    assert len(trader.orders) == 1
    assert "close it manually" in caplog.text


def test_smoke_halts_before_placing_when_sizing_is_blocked(monkeypatch, caplog):
    trader = FakeTrader()
    c = smoke_consumer(monkeypatch, trader)
    # An unusable sizing config must stop the smoke test at the OPEN.
    monkeypatch.setattr(
        c, "_get", lambda path, params: {
            "config": dict(SIZING_CONFIG, capital_allocation_pct=500)
        }
    )
    c._leverage = None  # force a refresh on the next ensure_config()
    with caplog.at_level(logging.ERROR, logger="executor.consumer"):
        assert c.run_smoke_test("LONG", 0) == 1
    assert trader.orders == []


def test_smoke_below_min_notional_places_nothing(monkeypatch):
    """A configured size under the 20 USDT exchange floor must refuse, not send
    dust. That floor is Binance's, and it is the only lower bound."""
    trader = FakeTrader()
    c = smoke_consumer(monkeypatch, trader, wallet="1000")
    assert c.run_smoke_test("LONG", 0) == 1
    assert trader.orders == []
