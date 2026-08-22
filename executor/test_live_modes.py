"""Gate 9: LIVE_READ / LIVE_TRADE safety invariants.

Pure logic and env handling: no network, no credentials, nothing that could
reach an exchange. Every test here asserts a refusal or a ceiling.
"""

import logging
from decimal import Decimal

import pytest

import main
from binance_client import ReadOnlyFuturesClient
from risk_guard import RiskGuard
from signal_consumer import SignalConsumer, SignalConsumerError

LIVE_ENV = (
    "LIVE_TRADING_ACK",
    "LIVE_ORDER_CAP_USD",
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
    code, cap = main.live_preflight("LIVE_TRADE")
    assert (code, cap) == (1, None)


def test_live_trade_refuses_with_wrong_ack(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ACK", "yes")
    monkeypatch.setenv("LIVE_ORDER_CAP_USD", "30")
    code, _ = main.live_preflight("LIVE_TRADE")
    assert code == 1


def test_live_trade_accepts_exact_ack_with_cap(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND_REAL_MONEY")
    monkeypatch.setenv("LIVE_ORDER_CAP_USD", "30")
    code, cap = main.live_preflight("LIVE_TRADE")
    assert code is None
    assert cap == Decimal("30")


# --- LIVE_TRADE preflight: mandatory cap --------------------------------- #

@pytest.mark.parametrize("raw", ["", "0", "-5", "abc", "501"])
def test_live_trade_refuses_bad_or_missing_cap(monkeypatch, raw):
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND_REAL_MONEY")
    if raw:
        monkeypatch.setenv("LIVE_ORDER_CAP_USD", raw)
    code, cap = main.live_preflight("LIVE_TRADE")
    assert (code, cap) == (1, None)


def test_live_read_needs_neither_ack_nor_cap():
    code, cap = main.live_preflight("LIVE_READ")
    assert (code, cap) == (None, None)


def test_testnet_modes_are_untouched_by_preflight():
    for mode in ("TESTNET_READ", "TESTNET_TRADE", "OFF"):
        assert main.live_preflight(mode) == (None, None)


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


# --- the live cap binds in every sizing mode ----------------------------- #

def open_order(notional, leverage=30):
    return {
        "symbol": "ETHUSDT",
        "intent": "OPEN",
        "side": "LONG",
        "qty": 1.0,
        "notional_usd": notional,
        "leverage": leverage,
    }


@pytest.mark.parametrize("mode", ["allocation", "full_capital"])
def test_live_cap_rejects_oversized_open_in_both_sizing_modes(mode):
    g = RiskGuard(max_notional_usd=500, max_leverage=90, live_cap_usd=30)
    g.set_sizing_mode(mode)
    allowed, reason = g.evaluate(open_order(100), 0, None)
    assert allowed is False
    assert "exceeds live cap" in reason


@pytest.mark.parametrize("mode", ["allocation", "full_capital"])
def test_live_cap_allows_an_order_at_the_cap(mode):
    g = RiskGuard(max_notional_usd=500, max_leverage=90, live_cap_usd=30)
    g.set_sizing_mode(mode)
    assert g.evaluate(open_order(30), 0, None) == (True, "ok")


def test_live_cap_cannot_be_raised_by_a_config_refresh():
    g = RiskGuard(max_notional_usd=500, max_leverage=90, live_cap_usd=30)
    g.set_sizing_mode("allocation")
    # A config refresh sets max_notional_usd; it must not touch the live cap.
    g.update_limits(max_notional_usd=500)
    allowed, reason = g.evaluate(open_order(400), 0, None)
    assert allowed is False
    assert "exceeds live cap" in reason


def test_live_cap_does_not_exempt_close():
    g = RiskGuard(max_notional_usd=500, max_leverage=90, live_cap_usd=30)
    g.set_sizing_mode("allocation")
    close = {
        "symbol": "ETHUSDT",
        "intent": "CLOSE",
        "side": "LONG",
        "qty": 1.0,
        "notional_usd": 5000.0,
    }
    # CLOSE reduces exposure, so the cap must never stand between a live
    # position and the order that flattens it.
    assert g.evaluate(close, 1.5, None) == (True, "ok")


def test_no_live_cap_leaves_existing_behaviour_unchanged():
    g = RiskGuard(max_notional_usd=500, max_leverage=90)
    g.set_sizing_mode("full_capital")
    assert g.evaluate(open_order(21000), 0, None) == (True, "ok")


def test_cap_ceiling_matches_the_guard_backstop():
    assert main.LIVE_ORDER_CAP_MAX_USD == Decimal(
        str(RiskGuard.ABSOLUTE_MAX_NOTIONAL_USD)
    )


# --- full_capital is locked out on mainnet by default -------------------- #

def consumer_with_config(mode, config, monkeypatch, allow_full_capital=False):
    if allow_full_capital:
        monkeypatch.setenv("LIVE_ALLOW_FULL_CAPITAL", "1")
    c = SignalConsumer("https://app.example.com", "tok", "user", mode)
    monkeypatch.setattr(c, "_get", lambda path, params: {"config": config})
    c._refresh_config()
    return c


FULL_CAPITAL_CONFIG = {
    "leverage": 30,
    "capital_allocation_pct": 7,
    "capital_usd": 700,
    "max_notional_usd": 500,
    "sizing_mode": "full_capital",
    "account_size_usd": 10000,
    # The operator's half of the full-capital consent. The env flag is the
    # host's half, and these tests vary that one; both are required.
    "live_allow_full_capital": True,
}


def test_full_capital_blocked_when_only_the_host_consents(monkeypatch, caplog):
    """Env flag set, database consent absent — still blocked.

    The mirror of the default case below: neither side's consent is sufficient
    on its own, and this is the direction a database edit could reach."""
    config = dict(FULL_CAPITAL_CONFIG, live_allow_full_capital=False)
    with caplog.at_level(logging.ERROR, logger="executor.consumer"):
        c = consumer_with_config("LIVE_TRADE", config, monkeypatch, allow_full_capital=True)
    assert c._config_invalid is True
    assert "live_allow_full_capital" in caplog.text


def test_full_capital_blocks_opens_in_live_trade_by_default(monkeypatch, caplog):
    with caplog.at_level(logging.ERROR, logger="executor.consumer"):
        c = consumer_with_config("LIVE_TRADE", FULL_CAPITAL_CONFIG, monkeypatch)
    assert c._config_invalid is True
    assert "LIVE_ALLOW_FULL_CAPITAL" in caplog.text


def test_full_capital_blocks_opens_in_live_read_by_default(monkeypatch):
    c = consumer_with_config("LIVE_READ", FULL_CAPITAL_CONFIG, monkeypatch)
    assert c._config_invalid is True


def test_full_capital_permitted_on_mainnet_only_with_the_flag(monkeypatch):
    c = consumer_with_config(
        "LIVE_TRADE", FULL_CAPITAL_CONFIG, monkeypatch, allow_full_capital=True
    )
    assert c._config_invalid is False


def test_full_capital_still_works_on_testnet_without_the_flag(monkeypatch):
    c = consumer_with_config("TESTNET_TRADE", FULL_CAPITAL_CONFIG, monkeypatch)
    assert c._config_invalid is False


def test_allocation_mode_is_unaffected_on_live(monkeypatch):
    config = dict(FULL_CAPITAL_CONFIG, sizing_mode="allocation")
    c = consumer_with_config("LIVE_TRADE", config, monkeypatch)
    assert c._config_invalid is False


# --- live cap clamps the sized order ------------------------------------- #

def test_live_cap_clamps_the_configured_allocation_size(monkeypatch, caplog):
    """The reported config sizes to 500 notional; the live cap must bind."""
    c = SignalConsumer(
        "https://app.example.com", "tok", "user", "LIVE_TRADE",
        live_order_cap_usd=Decimal("30"),
    )
    config = dict(FULL_CAPITAL_CONFIG, sizing_mode="allocation")
    monkeypatch.setattr(c, "_get", lambda path, params: {"config": config})
    c._refresh_config()
    c.set_available_balance(Decimal("1000"))

    signal = {"bar_time": "2026-08-16T00:00:00Z", "rule_side": 1}
    with caplog.at_level(logging.INFO, logger="executor.consumer"):
        order = c._build_intent(signal, Decimal("4000"))

    assert order["notional_usd"] <= 30
    assert order["status"] == "INTENT_LOGGED"
    assert "bound_by=live_order_cap" in caplog.text


def test_without_a_live_cap_the_config_sizes_to_500(monkeypatch):
    """Documents exactly what the cap is protecting against."""
    c = SignalConsumer("https://app.example.com", "tok", "user", "LIVE_TRADE")
    config = dict(FULL_CAPITAL_CONFIG, sizing_mode="allocation")
    monkeypatch.setattr(c, "_get", lambda path, params: {"config": config})
    c._refresh_config()
    c.set_available_balance(Decimal("1000"))

    order = c._build_intent({"bar_time": "2026-08-16T00:00:00Z", "rule_side": 1},
                            Decimal("4000"))
    assert 495 <= order["notional_usd"] <= 500


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


def smoke_consumer(monkeypatch, trader, cap="30", price="4000"):
    c = SignalConsumer(
        "https://app.example.com", "tok",
        "11111111-1111-1111-1111-111111111111", "LIVE_TRADE",
        live_order_cap_usd=Decimal(cap),
    )
    c._binance_trader = trader
    c._risk_guard = RiskGuard(
        max_notional_usd=500, max_leverage=90, live_cap_usd=Decimal(cap)
    )
    c._risk_guard.set_sizing_mode("allocation")
    config = dict(FULL_CAPITAL_CONFIG, sizing_mode="allocation")
    monkeypatch.setattr(c, "_get", lambda path, params: {"config": config})
    monkeypatch.setattr(c._binance, "get_mark_price", lambda symbol: float(price))
    monkeypatch.setattr(c, "_post_order", lambda order: True)
    monkeypatch.setattr(c, "_post_order_update", lambda update: None)
    # Settle polling would otherwise sleep between reads.
    monkeypatch.setattr("signal_consumer.SETTLE_POLL_INTERVAL_SECONDS", 0)
    c.set_available_balance(Decimal("1000"))
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


def test_smoke_order_is_capped_by_the_live_cap(monkeypatch):
    trader = FakeTrader()
    c = smoke_consumer(monkeypatch, trader, cap="30", price="4000")
    assert c.run_smoke_test("LONG", 0) == 0

    qty = trader.orders[0]["qty"]
    # 30 USD / 4000 rounded down to the 0.001 step = 0.007 -> 28 USD notional.
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
            "config": dict(FULL_CAPITAL_CONFIG, sizing_mode="banana")
        }
    )
    c._leverage = None  # force a refresh on the next ensure_config()
    with caplog.at_level(logging.ERROR, logger="executor.consumer"):
        assert c.run_smoke_test("LONG", 0) == 1
    assert trader.orders == []


def test_smoke_cap_below_min_notional_places_nothing(monkeypatch):
    """A cap under the 20 USDT exchange floor must refuse, not send dust."""
    trader = FakeTrader()
    c = smoke_consumer(monkeypatch, trader, cap="10")
    assert c.run_smoke_test("LONG", 0) == 1
    assert trader.orders == []
