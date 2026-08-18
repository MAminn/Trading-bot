"""Phase 0: executor telemetry. Pure logic and a stubbed session — no network.

The invariants under test are all containment invariants: telemetry reports
what is true, reports nothing it cannot substantiate, and cannot break a cycle.
"""

import logging

import pytest

from binance_client import BinanceAPIError
from executor_status import (
    AUTH_FAILURE_CODES,
    StatusReporter,
    build_snapshot,
    permission_status_for_error,
    position_side,
)

USER = "11111111-1111-1111-1111-111111111111"

ACCOUNT = {"totalWalletBalance": "1234.5", "availableBalance": "1000.25"}
POSITIONS = [
    {
        "positionAmt": "0.030",
        "entryPrice": "2500.10",
        "leverage": "30",
        "marginType": "isolated",
    }
]
RECONCILE = {
    "expected": 0.03,
    "match": True,
    "actual": 0.03,
    "at": "2026-08-17T10:00:00Z",
}


def snapshot(**overrides):
    kwargs = dict(
        mode="LIVE_TRADE",
        env_mode_ceiling="LIVE_TRADE",
        account=ACCOUNT,
        positions=POSITIONS,
        reconcile=RECONCILE,
        keys_present=True,
        permission_status="verified_futures",
        message="cycle ok",
    )
    kwargs.update(overrides)
    return build_snapshot(**kwargs)


# --- position side ------------------------------------------------------- #

@pytest.mark.parametrize(
    "amt,expected",
    [
        (0.03, "LONG"),
        (-0.03, "SHORT"),
        (0, "FLAT"),
        ("0.0", "FLAT"),
        (None, "FLAT"),
        # Below the reconciler's flat threshold: dust is flat, in the dashboard
        # exactly as in the reconcile decision.
        (0.0001, "FLAT"),
        (-0.0001, "FLAT"),
        ("nonsense", None),
    ],
)
def test_position_side(amt, expected):
    assert position_side(amt) == expected


# --- snapshot contents --------------------------------------------------- #

def test_snapshot_reads_account_and_position():
    s = snapshot()
    assert s["effective_mode"] == "LIVE_TRADE"
    assert s["env_mode_ceiling"] == "LIVE_TRADE"
    assert s["wallet_balance_usd"] == 1234.5
    assert s["available_balance_usd"] == 1000.25
    assert s["position_amt"] == 0.03
    assert s["position_side"] == "LONG"
    assert s["entry_price"] == 2500.10
    assert s["position_leverage"] == 30.0
    assert s["margin_type"] == "isolated"


def test_snapshot_reports_unknowns_as_none_never_zero():
    """A missing reading must not render as a real balance of $0.00."""
    s = snapshot(account=None, positions=None, reconcile=None)
    for field in (
        "wallet_balance_usd",
        "available_balance_usd",
        "position_amt",
        "position_side",
        "entry_price",
        "position_leverage",
        "margin_type",
        "reconcile_match",
        "reconcile_expected",
        "reconcile_actual",
        "last_reconcile_at",
    ):
        assert s[field] is None, field


def test_snapshot_always_carries_every_key():
    """A successful heartbeat overwrites the whole snapshot, so a stale value
    can never survive underneath a fresh one."""
    full = set(snapshot().keys())
    assert set(snapshot(account=None, positions=None, reconcile=None).keys()) == full


def test_unreadable_numbers_degrade_to_none():
    s = snapshot(
        account={"totalWalletBalance": "n/a", "availableBalance": None},
        positions=[{"positionAmt": "n/a", "entryPrice": "", "leverage": None}],
    )
    assert s["wallet_balance_usd"] is None
    assert s["available_balance_usd"] is None
    assert s["position_amt"] is None
    assert s["position_side"] is None
    assert s["entry_price"] is None


def test_reconcile_fields_are_copied():
    s = snapshot()
    assert s["reconcile_match"] is True
    assert s["reconcile_expected"] == 0.03
    assert s["reconcile_actual"] == 0.03
    assert s["last_reconcile_at"] == "2026-08-17T10:00:00Z"


def test_reconcile_timestamp_must_be_a_string():
    """A non-serialisable timestamp is dropped rather than sent as garbage."""
    s = snapshot(reconcile={"expected": 0.0, "match": False, "actual": 0.0, "at": object()})
    assert s["last_reconcile_at"] is None
    assert s["reconcile_match"] is False


def test_message_is_truncated():
    s = snapshot(message="x" * 5000)
    assert len(s["message"]) == 500


def test_snapshot_carries_no_credential_material():
    """Only presence and a permission verdict — never a key, ever."""
    s = snapshot()
    assert s["keys_present"] is True
    assert s["permission_status"] == "verified_futures"
    text = repr(s).lower()
    for forbidden in ("api_key", "api_secret", "apikey", "secret", "signature"):
        assert forbidden not in text


def test_snapshot_never_fabricates_a_control_state():
    """Phase 3 reports the control values; it must never invent them.

    These fields are a REPORT of a decision made elsewhere, never an input to
    one. When the caller supplies nothing, they read None rather than
    defaulting to a value that would describe a state nobody chose."""
    s = snapshot()
    for control in (
        "db_execution_mode",
        "auto_execute_enabled",
        "live_order_cap_usd",
        "live_order_cap_env_max",
        "orders_enabled",
        "blocked_reason",
    ):
        assert control in s, control
        assert s[control] is None, control


def test_snapshot_reports_the_control_values_it_is_given():
    s = snapshot(
        db_execution_mode="LIVE_TRADE",
        auto_execute_enabled=True,
        live_order_cap_usd=30,
        live_order_cap_env_max=30,
        orders_enabled=False,
        blocked_reason="auto_execute_disabled",
    )
    assert s["db_execution_mode"] == "LIVE_TRADE"
    assert s["auto_execute_enabled"] is True
    assert s["live_order_cap_usd"] == 30.0
    assert s["live_order_cap_env_max"] == 30.0
    assert s["orders_enabled"] is False
    assert s["blocked_reason"] == "auto_execute_disabled"


def test_effective_and_requested_modes_are_reported_separately():
    """A degraded request must be visible as a degradation, not hidden behind
    a single 'mode' field that could be read as either."""
    s = snapshot(mode="LIVE_READ", env_mode_ceiling="LIVE_READ", db_execution_mode="LIVE_TRADE")
    assert s["effective_mode"] == "LIVE_READ"
    assert s["env_mode_ceiling"] == "LIVE_READ"
    assert s["db_execution_mode"] == "LIVE_TRADE"


# --- permission status --------------------------------------------------- #

@pytest.mark.parametrize("code", sorted(AUTH_FAILURE_CODES))
def test_credential_rejection_is_reported_as_failed(code):
    assert permission_status_for_error(BinanceAPIError(401, code, "bad key")) == "failed"


@pytest.mark.parametrize(
    "exc",
    [
        BinanceAPIError(500, -1001, "internal"),
        BinanceAPIError(400, None, "no code"),
        OSError("connection reset"),
        ValueError("nonsense"),
    ],
)
def test_other_failures_stay_unknown(exc):
    """A permission claim we cannot substantiate is never asserted."""
    assert permission_status_for_error(exc) == "unknown"


# --- reporter containment ------------------------------------------------ #

class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeSession:
    def __init__(self, behaviour):
        self._behaviour = behaviour
        self.calls = []
        self.headers = {}

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if isinstance(self._behaviour, Exception):
            raise self._behaviour
        return FakeResponse(self._behaviour)


def make_reporter(behaviour):
    r = StatusReporter("http://app.invalid/", "token", USER)
    r._session = FakeSession(behaviour)
    return r


def test_report_posts_user_id_and_snapshot():
    r = make_reporter(200)
    assert r.report(snapshot()) is True
    call = r._session.calls[0]
    assert call["url"] == "http://app.invalid/api/public/engine/ingest/executor_status"
    assert call["json"]["user_id"] == USER
    assert call["json"]["effective_mode"] == "LIVE_TRADE"


def test_bearer_token_is_sent_but_never_in_the_body():
    r = StatusReporter("http://app.invalid", "supersecrettoken", USER)
    assert r._session.headers["Authorization"] == "Bearer supersecrettoken"
    r._session = FakeSession(200)
    r.report(snapshot())
    assert "supersecrettoken" not in repr(r._session.calls[0]["json"])


@pytest.mark.parametrize(
    "behaviour",
    [
        404,  # app deployed without the route yet
        500,  # migration not applied yet
        503,
        401,
        OSError("connection refused"),
        ValueError("unserialisable payload"),
    ],
)
def test_every_failure_is_contained(behaviour):
    """Telemetry must never be able to fail a trading cycle."""
    r = make_reporter(behaviour)
    assert r.report(snapshot()) is False  # returns, does not raise


def test_repeated_failures_are_logged_once_then_suppressed(caplog):
    r = make_reporter(404)
    with caplog.at_level(logging.WARNING, logger="executor.status"):
        for _ in range(10):
            r.report(snapshot())
    warnings = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "trading is unaffected" in warnings[0].getMessage()


# --- consumer records the reconcile it just performed -------------------- #

def test_consumer_records_last_reconcile_for_telemetry():
    from signal_consumer import SignalConsumer

    c = SignalConsumer("http://app.invalid", "token", USER, "TESTNET_READ", "ETHUSDT")
    assert c.last_reconcile is None
    c._reconciler.reconcile = lambda amt: {
        "expected": 0.03,
        "match": True,
        "is_running": True,
        "stale_intents": [],
    }
    c.reconcile(0.03)
    assert c.last_reconcile["expected"] == 0.03
    assert c.last_reconcile["match"] is True
    assert c.last_reconcile["actual"] == 0.03
    assert c.last_reconcile["at"].endswith("Z")


def test_failed_reconcile_leaves_no_fresh_telemetry():
    """A reconcile that could not be fetched must not stamp a new timestamp on
    the previous reading."""
    from reconciler import ReconcilerError
    from signal_consumer import SignalConsumer

    c = SignalConsumer("http://app.invalid", "token", USER, "TESTNET_READ", "ETHUSDT")

    def boom(amt):
        raise ReconcilerError("orders/state unreachable")

    c._reconciler.reconcile = boom
    # Read-only mode swallows the failure; either way nothing is recorded.
    c.reconcile(0.0)
    assert c.last_reconcile is None


def test_recovery_is_logged_and_resets_the_counter(caplog):
    r = make_reporter(404)
    r.report(snapshot())
    assert r._failures == 1
    r._session = FakeSession(200)
    with caplog.at_level(logging.INFO, logger="executor.status"):
        assert r.report(snapshot()) is True
    assert r._failures == 0
    assert "recovered" in caplog.text
