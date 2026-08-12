"""Gate 3: _refresh_config logic. The app API is stubbed; no network."""

import logging
from decimal import Decimal

import pytest

from signal_consumer import HARD_CAP_USD, SignalConsumer
from risk_guard import RiskGuard

USER = "11111111-1111-1111-1111-111111111111"

BASE_CONFIG = {
    "leverage": 30,
    "capital_allocation_pct": 6,
    "capital_usd": 100,
    "max_notional_usd": 500,
    "sizing_mode": "allocation",
    "account_size_usd": 1667,
}


def make_consumer(config, with_guard=False):
    guard = RiskGuard(max_notional_usd=100, max_leverage=90) if with_guard else None
    c = SignalConsumer(
        "http://app.invalid", "token", USER, "TESTNET_READ", "ETHUSDT",
        risk_guard=guard,
    )
    c._get = lambda path, params: {"config": config}
    return c, guard


def test_valid_allocation_config_refreshes_clean(caplog):
    c, _ = make_consumer(dict(BASE_CONFIG))
    with caplog.at_level(logging.INFO, logger="executor.consumer"):
        c._refresh_config()
    assert c._config_invalid is False
    assert c._sizing_mode == "allocation"
    assert c._account_size_usd == Decimal("1667")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "sizing | mode=allocation" in caplog.text


def test_valid_full_capital_config_refreshes_clean(caplog):
    cfg = dict(BASE_CONFIG, sizing_mode="full_capital", account_size_usd=750)
    c, _ = make_consumer(cfg)
    with caplog.at_level(logging.INFO, logger="executor.consumer"):
        c._refresh_config()
    assert c._config_invalid is False
    assert c._sizing_mode == "full_capital"
    assert c._account_size_usd == Decimal("750")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "sizing | mode=full_capital" in caplog.text


def test_absent_sizing_mode_defaults_to_allocation_at_info(caplog):
    cfg = {k: v for k, v in BASE_CONFIG.items() if k != "sizing_mode"}
    c, _ = make_consumer(cfg)
    with caplog.at_level(logging.INFO, logger="executor.consumer"):
        c._refresh_config()
    assert c._sizing_mode == "allocation"
    assert c._config_invalid is False
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "defaulting to allocation" in caplog.text


def test_invalid_sizing_mode_sets_config_invalid_and_logs_error(caplog):
    c, _ = make_consumer(dict(BASE_CONFIG, sizing_mode="banana"))
    with caplog.at_level(logging.ERROR, logger="executor.consumer"):
        c._refresh_config()
    assert c._config_invalid is True
    # Fails closed to the narrower mode rather than guessing.
    assert c._sizing_mode == "allocation"
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.parametrize("bad", [None, 0, -100])
def test_full_capital_requires_positive_account_size(bad, caplog):
    cfg = dict(BASE_CONFIG, sizing_mode="full_capital", account_size_usd=bad)
    c, _ = make_consumer(cfg)
    with caplog.at_level(logging.ERROR, logger="executor.consumer"):
        c._refresh_config()
    assert c._config_invalid is True
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_allocation_tolerates_missing_account_size():
    cfg = {k: v for k, v in BASE_CONFIG.items() if k != "account_size_usd"}
    c, _ = make_consumer(cfg)
    c._refresh_config()
    assert c._config_invalid is False
    assert c._account_size_usd is None


def test_recovers_without_restart_after_config_is_corrected():
    state = {"cfg": dict(BASE_CONFIG, sizing_mode="banana")}
    c, _ = make_consumer(None)
    c._get = lambda path, params: {"config": state["cfg"]}

    c._refresh_config()
    assert c._config_invalid is True

    state["cfg"] = dict(BASE_CONFIG, sizing_mode="full_capital", account_size_usd=750)
    c._refresh_config()
    assert c._config_invalid is False
    assert c._sizing_mode == "full_capital"


def test_missing_max_position_size_usd_no_longer_raises():
    """The latent crash removed in 3a: the field is gone from the payload."""
    cfg = {k: v for k, v in BASE_CONFIG.items()}
    cfg.pop("max_position_size_usd", None)
    c, _ = make_consumer(cfg)
    c._refresh_config()
    assert c._config_invalid is False
    assert not hasattr(c, "_max_position_size_usd")


def test_missing_max_notional_falls_back_to_hard_cap(caplog):
    cfg = {k: v for k, v in BASE_CONFIG.items() if k != "max_notional_usd"}
    c, _ = make_consumer(cfg)
    with caplog.at_level(logging.INFO, logger="executor.consumer"):
        c._refresh_config()
    assert c._max_notional_usd == HARD_CAP_USD
    assert c._config_invalid is False


def test_mode_is_pushed_to_risk_guard():
    cfg = dict(BASE_CONFIG, sizing_mode="full_capital", account_size_usd=750)
    c, guard = make_consumer(cfg, with_guard=True)
    c._refresh_config()
    assert guard._sizing_mode == "full_capital"


def test_guard_gets_allocation_when_mode_is_invalid():
    c, guard = make_consumer(dict(BASE_CONFIG, sizing_mode="banana"), with_guard=True)
    c._refresh_config()
    assert guard._sizing_mode == "allocation"


def test_config_response_body_is_never_logged(caplog):
    """The config payload carries decrypted Binance credentials."""
    cfg = dict(BASE_CONFIG, binance_api_key="SECRETKEY", binance_api_secret="SECRETSEC")
    c, _ = make_consumer(cfg)
    with caplog.at_level(logging.DEBUG, logger="executor.consumer"):
        c._refresh_config()
    assert "SECRETKEY" not in caplog.text
    assert "SECRETSEC" not in caplog.text


def test_leverage_change_reselects_bracket():
    ladder = [
        {"initialLeverage": 90, "notionalCap": Decimal("50000")},
        {"initialLeverage": 5, "notionalCap": Decimal("20000000")},
    ]
    state = {"cfg": dict(BASE_CONFIG, leverage=90)}
    c, _ = make_consumer(None)
    c._get = lambda path, params: {"config": state["cfg"]}
    c._refresh_config()
    c.set_leverage_limits(90, ladder)
    assert c._bracket_notional_cap == Decimal("50000")

    # Leverage drops on a later refresh: the applicable tier must widen.
    state["cfg"] = dict(BASE_CONFIG, leverage=5)
    c._refresh_config()
    assert c._bracket_notional_cap == Decimal("20000000")
