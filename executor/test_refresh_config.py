"""_refresh_config logic. The app API is stubbed; no network.

The config carries exactly two sizing inputs — capital_allocation_pct and
leverage — and they are read independently. There is no sizing mode, no account
size and no capital figure: the capital base is the exchange's own
totalWalletBalance and is deliberately not representable in this payload.
"""

import inspect
import logging
from decimal import Decimal

import pytest

import signal_consumer
from signal_consumer import SignalConsumer, SignalConsumerError
from risk_guard import RiskGuard

USER = "11111111-1111-1111-1111-111111111111"

BASE_CONFIG = {
    "leverage": 30,
    "capital_allocation_pct": 20,
    # Live controls, as a current engine_config row carries them. Present but
    # closed: this fixture must describe a real row, and a real row always has
    # these columns since the Phase 1 migration.
    "execution_mode": "OFF",
    "auto_execute_enabled": False,
    "is_running": False,
}


def make_consumer(config, with_guard=False):
    guard = RiskGuard(max_leverage=90) if with_guard else None
    c = SignalConsumer(
        "http://app.invalid", "token", USER, "TESTNET_READ", "ETHUSDT",
        risk_guard=guard,
    )
    c._get = lambda path, params: {"config": config}
    return c, guard


def test_valid_config_refreshes_clean(caplog):
    c, _ = make_consumer(dict(BASE_CONFIG))
    with caplog.at_level(logging.INFO, logger="executor.consumer"):
        c._refresh_config()
    assert c._config_invalid is False
    assert c._alloc_pct == Decimal("20")
    assert c._leverage == Decimal("30")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "sizing | wallet_balance=" in caplog.text


def test_allocation_and_leverage_are_read_independently():
    """Neither field's value influences the other's, in either direction."""
    c, _ = make_consumer(dict(BASE_CONFIG, capital_allocation_pct=1, leverage=90))
    c._refresh_config()
    assert c._alloc_pct == Decimal("1")
    assert c._leverage == Decimal("90")

    c, _ = make_consumer(dict(BASE_CONFIG, capital_allocation_pct=100, leverage=1))
    c._refresh_config()
    assert c._alloc_pct == Decimal("100")
    assert c._leverage == Decimal("1")


@pytest.mark.parametrize("pct", [1, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
def test_every_permitted_allocation_is_accepted(pct):
    c, _ = make_consumer(dict(BASE_CONFIG, capital_allocation_pct=pct))
    c._refresh_config()
    assert c._config_invalid is False
    assert c._alloc_pct == Decimal(str(pct))


@pytest.mark.parametrize("lev", [1, 10, 20, 30, 40, 50, 60, 70, 80, 90])
def test_every_permitted_leverage_is_accepted(lev):
    c, _ = make_consumer(dict(BASE_CONFIG, leverage=lev))
    c._refresh_config()
    assert c._config_invalid is False
    assert c._leverage == Decimal(str(lev))


@pytest.mark.parametrize("bad", [0, -5, 101, 1000])
def test_out_of_range_allocation_blocks_opens(bad, caplog):
    c, _ = make_consumer(dict(BASE_CONFIG, capital_allocation_pct=bad))
    with caplog.at_level(logging.ERROR, logger="executor.consumer"):
        c._refresh_config()
    assert c._config_invalid is True
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_leverage_blocks_opens(bad, caplog):
    c, _ = make_consumer(dict(BASE_CONFIG, leverage=bad))
    with caplog.at_level(logging.ERROR, logger="executor.consumer"):
        c._refresh_config()
    assert c._config_invalid is True


@pytest.mark.parametrize("field", ["leverage", "capital_allocation_pct"])
def test_missing_sizing_field_raises_rather_than_guessing(field):
    cfg = {k: v for k, v in BASE_CONFIG.items() if k != field}
    c, _ = make_consumer(cfg)
    with pytest.raises(SignalConsumerError, match=field):
        c._refresh_config()


def test_recovers_without_restart_after_config_is_corrected():
    state = {"cfg": dict(BASE_CONFIG, capital_allocation_pct=500)}
    c, _ = make_consumer(None)
    c._get = lambda path, params: {"config": state["cfg"]}

    c._refresh_config()
    assert c._config_invalid is True

    state["cfg"] = dict(BASE_CONFIG, capital_allocation_pct=50)
    c._refresh_config()
    assert c._config_invalid is False
    assert c._alloc_pct == Decimal("50")


def test_missing_max_position_size_usd_no_longer_raises():
    cfg = {k: v for k, v in BASE_CONFIG.items()}
    cfg.pop("max_position_size_usd", None)
    c, _ = make_consumer(cfg)
    c._refresh_config()
    assert c._config_invalid is False
    assert not hasattr(c, "_max_position_size_usd")


def test_legacy_sizing_columns_in_the_payload_are_ignored():
    """A row or endpoint that still serves the old fields must not be able to
    resurrect the old behaviour. They are read by nothing."""
    cfg = dict(
        BASE_CONFIG,
        sizing_mode="full_capital",
        account_size_usd=1_000_000,
        capital_usd=1_000_000,
        max_notional_usd=500,
        live_allow_full_capital=True,
    )
    c, _ = make_consumer(cfg)
    c._refresh_config()
    assert c._config_invalid is False
    assert c._alloc_pct == Decimal("20")
    assert c._leverage == Decimal("30")
    for attr in (
        "_sizing_mode",
        "_account_size_usd",
        "_capital_usd",
        "_max_notional_usd",
        "_live_allow_full_capital_db",
        "_live_allow_full_capital_env",
    ):
        assert not hasattr(c, attr), f"{attr} still exists on the consumer"


def test_a_refresh_pushes_no_size_limit_to_the_risk_guard():
    """A config refresh can no longer hand the guard ANY notional limit: no
    sizing mode, no max notional, no operator dollar cap. The guard's only
    config-derived limit is the exchange leverage maximum, and that comes from
    the bracket probe rather than from the database."""
    c, guard = make_consumer(dict(BASE_CONFIG), with_guard=True)
    before = dict(vars(guard))
    c._refresh_config()
    assert vars(guard) == before
    for attr in ("_sizing_mode", "_max_notional_usd", "_live_cap_usd", "_live_cap_ceiling"):
        assert not hasattr(guard, attr), attr


def test_refreshes_from_the_payload_that_carries_no_key_material():
    """Phase 0 removed the decrypted `binance` block from the config endpoint;
    it now returns presence metadata instead. The consumer never read the
    credentials — lock that in so the endpoint can never be "fixed" back."""
    c, _ = make_consumer(None)
    c._get = lambda path, params: {
        "config": dict(BASE_CONFIG),
        "keys_present": True,
        "api_key_last4": "ab12",
    }
    c._refresh_config()
    assert c._config_invalid is False
    assert c._leverage == Decimal("30")


def test_config_response_body_is_never_logged(caplog):
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
    state["cfg"] = dict(BASE_CONFIG, leverage=1)
    c._refresh_config()
    assert c._bracket_notional_cap == Decimal("20000000")


def test_no_sizing_mode_concept_survives_the_module():
    src = inspect.getsource(signal_consumer)
    for banned in ("sizing_mode", "full_capital", "account_size", "HARD_CAP_USD"):
        assert banned not in src
