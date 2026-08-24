"""The maintenance gate: OPENs blocked, CLOSEs still executable.

This is the safety property the rollout depends on. Step 2 of the rollout sets
`mode = 'signal_only'` (so `auto_execute_enabled` becomes false) to stop new
entries while the app, executor and schema are replaced. That is only safe if a
client who is ALREADY holding a real Binance position can still be flattened —
by the strategy's own CLOSE signal, and by reconciliation.

Nothing here is a unit test of a gate in isolation. Every test drives the real
`UserSession.run_cycle()` with the real `SignalConsumer`, the real `RiskGuard`
and the real `Reconciler`. Exactly two boundaries are faked:

  * the app's HTTP API  (config, pending signals, orders/state, ingest)
  * the Binance client  (account, positions, place_market_order)

so the gate chain under test is the production one:

    engine_config.mode='signal_only'
      -> auto_execute_enabled = false        (generated column)
      -> SignalConsumer._db_auto_execute_enabled
      -> live_controls.placement_block_reason() -> "auto_execute_disabled"
      -> UserSession._blocked_reason -> opens_blocked=True
      -> SignalConsumer._process_intent(... opens_blocked ...)
           applies the block ONLY when order["intent"] == "OPEN"

and, critically, the placement client is attached on a path that never consults
auto-execute at all:

    UserSession.run_cycle: set_trader(client if effective_trade_capable ...)
    effective_trade_capable = is_trade_capable(
        resolve_effective_mode(env_mode, db_execution_mode))
"""

import logging

import pytest

from risk_guard import RiskGuard
from user_session import HostLimits, UserSession

SYMBOL = "ETHUSDT"
USER = "aaaaaaaa-1111-1111-1111-111111111111"
BAR = "2026-08-24T00:00:00Z"

# A client mid-trade: 0.5 ETH long, opened before the maintenance window.
OPEN_POSITION_AMT = "0.5"

# `mode='signal_only'` in the database. auto_execute_enabled is GENERATED from
# it, so this is exactly the row the rollout step produces.
MAINTENANCE_CONFIG = {
    "leverage": 30,
    "capital_allocation_pct": 20,
    "execution_mode": "LIVE_TRADE",
    "mode": "signal_only",
    "auto_execute_enabled": False,
    "is_running": True,
}

# The strategy's own flatten signal, shaped as the pending route serves it.
CLOSE_SIGNAL = {
    "bar_time": BAR,
    "created_at": BAR,
    "rule_side": 0,
    "ml_accept": True,
    "opened": None,
    "closed_reason": "trailing_stop",
    "position_before": "LONG",
    "position_after": "FLAT",
    "trade_id": "t-1",
}

# An accepted entry signal, which must NOT be executed during maintenance.
OPEN_SIGNAL = {
    "bar_time": BAR,
    "created_at": BAR,
    "rule_side": 1,
    "ml_accept": True,
    "opened": "LONG",
    "closed_reason": None,
    "position_before": "FLAT",
    "position_after": "LONG",
    "trade_id": "t-2",
}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeAppSession:
    """The app's HTTP API. Records every POST so a test can prove what the
    executor reported, and serves orders/state for the real Reconciler."""

    def __init__(self, config, signals, last_executed=None):
        self.headers = {}
        self.config = config
        self.signals = signals
        self.last_executed = last_executed
        self.posts: list = []

    def get(self, url, params=None, timeout=None):
        if url.endswith("/engine/config"):
            return FakeResponse({"config": dict(self.config)})
        if url.endswith("/signals/pending"):
            return FakeResponse(list(self.signals))
        if url.endswith("/orders/state"):
            return FakeResponse(
                {
                    "last_executed": self.last_executed,
                    "is_running": True,
                    "stale_intents": [],
                }
            )
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, json=None, timeout=None):
        self.posts.append((url.rsplit("/", 1)[-1], json))
        return FakeResponse({"ok": True}, status_code=201)


class FakeBinance:
    """One real Binance account. Refuses any write that is not a placement, and
    records every order so a test can assert reduceOnly."""

    def __init__(self, position_amt=OPEN_POSITION_AMT, wallet="300"):
        self.clock_offset_ms = 0
        self.position_amt = position_amt
        self.wallet = wallet
        self.orders: list = []

    def sync_clock(self):
        return 0

    def get_exchange_info(self, symbol):
        return {"filters": []}

    def get_account(self):
        return {"totalWalletBalance": self.wallet, "availableBalance": self.wallet}

    def get_positions(self, symbol):
        return [
            {
                "symbol": symbol,
                "positionAmt": self.position_amt,
                # Matches the configured 30x, so the leverage path is not what
                # blocks anything in these tests.
                "leverage": "30",
                "marginType": "isolated",
            }
        ]

    def get_leverage_brackets(self, symbol):
        return [{"initialLeverage": 90, "notionalCap": "50000"}]

    def set_leverage(self, symbol, leverage):  # pragma: no cover
        raise AssertionError("no leverage write may happen with a position open")

    def set_margin_type(self, symbol, margin_type):  # pragma: no cover
        raise AssertionError("no margin write may happen with a position open")

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
        # Reflect the fill so the executor's settle polling terminates at once,
        # exactly as a real filled MARKET order would.
        if reduce_only:
            self.position_amt = "0"
        else:
            delta = float(qty) if side == "BUY" else -float(qty)
            self.position_amt = str(round(float(self.position_amt) + delta, 8))
        return {"orderId": 4242, "status": "FILLED"}


class FakeCredentials:
    def __init__(self, user_id):
        self.user_id = user_id

    def fetch(self):
        from user_credentials import CredentialsResult, UserCredentials

        return CredentialsResult(
            credentials=UserCredentials(f"key-{self.user_id}", "secret", "abcd")
        )


def build_session(monkeypatch, *, config, signals, client, last_executed=None):
    """A real UserSession on a real LIVE_TRADE host, wired to the two fakes."""
    limits = HostLimits(
        env_mode="LIVE_TRADE",
        base_url="https://fapi.binance.com",
        trade_capable=True,
        ack_present=True,
        app_api_base="https://app.example.test",
        engine_service_token="service-token",
        is_live=True,
    )
    session = UserSession(
        user_id=USER,
        limits=limits,
        credentials_client=FakeCredentials(USER),
        env_credentials=None,
        reporter=None,
        start_after=None,
        symbol=SYMBOL,
        smoke_side=None,
    )
    app = FakeAppSession(config, signals, last_executed)
    # The consumer and the Reconciler share one session object in production;
    # both references are replaced so the real Reconciler runs against the fake.
    session._consumer._session = app
    session._consumer._reconciler._session = app
    monkeypatch.setattr(session, "_build_client", lambda key, secret: client)
    # Settle polling would otherwise sleep between position reads.
    monkeypatch.setattr("signal_consumer.SETTLE_POLL_INTERVAL_SECONDS", 0)
    return session, app


def ingested(app, kind):
    """Every payload POSTed to ingest/<kind>."""
    return [body for name, body in app.posts if name == kind]


# ------------------------------------------------------------------ #
# 1. The gate is real: auto-execute off blocks every NEW OPEN
# ------------------------------------------------------------------ #

def test_auto_execute_off_blocks_a_new_open(monkeypatch, caplog):
    """A fully accepted ML entry signal, on a flat account, with every other
    gate open. Nothing may reach Binance."""
    client = FakeBinance(position_amt="0")
    session, app = build_session(
        monkeypatch, config=MAINTENANCE_CONFIG, signals=[OPEN_SIGNAL], client=client
    )
    with caplog.at_level(logging.INFO, logger="executor.consumer"):
        session.run_cycle()

    assert client.orders == [], "an OPEN reached Binance during maintenance"
    assert session.blocked_reason == "auto_execute_disabled"
    assert session.orders_enabled is False

    opens = [o for o in ingested(app, "order") if o["intent"] == "OPEN"]
    assert len(opens) == 1
    assert opens[0]["status"] == "SKIPPED"
    assert opens[0]["error"] == "auto_execute_disabled"
    assert "BLOCKED | OPEN LONG | auto_execute_disabled" in caplog.text


def test_the_same_open_is_placed_once_auto_execute_is_on(monkeypatch):
    """The control test. Without it, the block above could be passing for any
    unrelated reason."""
    client = FakeBinance(position_amt="0")
    session, app = build_session(
        monkeypatch,
        config=dict(MAINTENANCE_CONFIG, mode="auto", auto_execute_enabled=True),
        signals=[OPEN_SIGNAL],
        client=client,
    )
    session.run_cycle()

    assert len(client.orders) == 1
    assert client.orders[0]["side"] == "BUY"
    assert client.orders[0]["reduce_only"] is False
    assert session.orders_enabled is True


# ------------------------------------------------------------------ #
# 2. The gate does NOT block a CLOSE of an already-open position
# ------------------------------------------------------------------ #

def test_auto_execute_off_still_closes_a_real_open_position(monkeypatch):
    """THE rollout-safety property.

    A client is holding 0.5 ETH long. Auto-execute is off. The strategy emits
    its own flatten signal. That CLOSE must reach Binance as a reduceOnly order
    sized from the ACTUAL position amount."""
    client = FakeBinance(position_amt=OPEN_POSITION_AMT)
    session, app = build_session(
        monkeypatch,
        config=MAINTENANCE_CONFIG,
        signals=[CLOSE_SIGNAL],
        client=client,
        # The app's record agrees with Binance, so reconcile matches and the
        # ONLY thing blocking OPENs is auto-execute.
        last_executed={"intent": "OPEN", "side": "LONG", "qty": 0.5},
    )
    session.run_cycle()

    assert session.blocked_reason == "auto_execute_disabled"
    assert session.orders_enabled is False

    # ... and the CLOSE went through anyway.
    assert len(client.orders) == 1, "the CLOSE did not reach Binance"
    close = client.orders[0]
    assert close["side"] == "SELL"          # closing a LONG sells
    assert close["reduce_only"] is True     # can never flip the position
    assert close["qty"] == 0.5              # the real Binance position amount

    closes = [o for o in ingested(app, "order") if o["intent"] == "CLOSE"]
    assert len(closes) == 1
    assert closes[0]["status"] == "INTENT_LOGGED"
    assert closes[0].get("error") is None

    updates = ingested(app, "order_update")
    assert any(u["status"] in ("SENT", "FILLED") for u in updates)


def test_a_short_position_is_also_closable_during_maintenance(monkeypatch):
    client = FakeBinance(position_amt="-0.5")
    session, app = build_session(
        monkeypatch,
        config=MAINTENANCE_CONFIG,
        signals=[dict(CLOSE_SIGNAL, position_before="SHORT")],
        client=client,
        last_executed={"intent": "OPEN", "side": "SHORT", "qty": 0.5},
    )
    session.run_cycle()

    assert len(client.orders) == 1
    assert client.orders[0]["side"] == "BUY"   # closing a SHORT buys
    assert client.orders[0]["reduce_only"] is True
    assert client.orders[0]["qty"] == 0.5


def test_a_reversal_signal_closes_but_does_not_re_enter(monkeypatch):
    """A signal carrying BOTH a flatten and a new entry. Exactly the dangerous
    case: the CLOSE half must execute and the OPEN half must not."""
    reversal = dict(
        CLOSE_SIGNAL,
        rule_side=-1,
        opened="SHORT",
        position_after="FLAT",
        closed_reason="reversal",
    )
    client = FakeBinance(position_amt=OPEN_POSITION_AMT)
    session, app = build_session(
        monkeypatch,
        config=MAINTENANCE_CONFIG,
        signals=[reversal],
        client=client,
        last_executed={"intent": "OPEN", "side": "LONG", "qty": 0.5},
    )
    session.run_cycle()

    assert len(client.orders) == 1
    assert client.orders[0]["reduce_only"] is True

    kinds = {o["intent"]: o for o in ingested(app, "order")}
    assert kinds["CLOSE"]["status"] == "INTENT_LOGGED"
    assert kinds["OPEN"]["status"] == "SKIPPED"
    assert kinds["OPEN"]["error"] == "auto_execute_disabled"


def test_the_close_survives_the_kill_switch_and_the_stop_button(monkeypatch):
    """Auto-execute is not the only maintenance control an operator might use.
    Pressing Stop (is_running=false) must also leave a position closable."""
    client = FakeBinance(position_amt=OPEN_POSITION_AMT)
    session, app = build_session(
        monkeypatch,
        # Auto-execute ON, so the ONLY thing blocking OPENs is the Stop switch.
        # placement_block_reason checks auto-execute first, and reports the most
        # fundamental reason, so leaving it off would test the wrong gate.
        config=dict(
            MAINTENANCE_CONFIG, mode="auto", auto_execute_enabled=True, is_running=False
        ),
        signals=[CLOSE_SIGNAL],
        client=client,
        last_executed={"intent": "OPEN", "side": "LONG", "qty": 0.5},
    )
    session.run_cycle()

    assert session.blocked_reason == "kill_switch_active"
    assert len(client.orders) == 1
    assert client.orders[0]["reduce_only"] is True


# ------------------------------------------------------------------ #
# 3. Reconciliation and flattening remain active
# ------------------------------------------------------------------ #

def test_reconciliation_still_runs_with_auto_execute_off(monkeypatch, caplog):
    client = FakeBinance(position_amt=OPEN_POSITION_AMT)
    session, app = build_session(
        monkeypatch,
        config=MAINTENANCE_CONFIG,
        signals=[],
        client=client,
        last_executed={"intent": "OPEN", "side": "LONG", "qty": 0.5},
    )
    with caplog.at_level(logging.INFO, logger="executor.consumer"):
        session.run_cycle()

    assert "RECONCILE | match=True" in caplog.text
    recon = session._consumer.last_reconcile
    assert recon is not None
    assert recon["match"] is True
    assert recon["actual"] == 0.5


def test_a_reconcile_mismatch_is_still_detected_during_maintenance(monkeypatch, caplog):
    """The app thinks the client is flat; Binance says 0.5 long. That must be
    seen and reported even while auto-execute is off — a maintenance window is
    exactly when an unnoticed drift would be worst."""
    client = FakeBinance(position_amt=OPEN_POSITION_AMT)
    session, app = build_session(
        monkeypatch,
        config=MAINTENANCE_CONFIG,
        signals=[],
        client=client,
        last_executed=None,  # app believes: flat
    )
    with caplog.at_level(logging.INFO, logger="executor.consumer"):
        session.run_cycle()

    assert "RECONCILE | match=False" in caplog.text
    assert session._consumer.last_reconcile["match"] is False


def test_a_mismatch_blocks_opens_and_still_permits_the_close(monkeypatch):
    client = FakeBinance(position_amt=OPEN_POSITION_AMT)
    session, app = build_session(
        monkeypatch,
        config=MAINTENANCE_CONFIG,
        signals=[CLOSE_SIGNAL],
        client=client,
        last_executed=None,  # mismatch: app flat, exchange long
    )
    session.run_cycle()

    assert len(client.orders) == 1
    assert client.orders[0]["reduce_only"] is True


def test_stale_intents_are_still_closed_out_during_maintenance(monkeypatch):
    """Reconciliation's housekeeping write must keep running: a stuck
    INTENT_LOGGED left behind by the window would misreport the account."""
    client = FakeBinance(position_amt=OPEN_POSITION_AMT)
    session, app = build_session(
        monkeypatch,
        config=MAINTENANCE_CONFIG,
        signals=[],
        client=client,
        last_executed={"intent": "OPEN", "side": "LONG", "qty": 0.5},
    )
    app.get_orig = app.get

    def get_with_stale(url, params=None, timeout=None):
        if url.endswith("/orders/state"):
            return FakeResponse(
                {
                    "last_executed": {"intent": "OPEN", "side": "LONG", "qty": 0.5},
                    "is_running": True,
                    "stale_intents": ["stuck-key-1"],
                }
            )
        return app.get_orig(url, params=params, timeout=timeout)

    app.get = get_with_stale
    session.run_cycle()

    updates = ingested(app, "order_update")
    assert any(
        u["idempotency_key"] == "stuck-key-1" and u["status"] == "FAILED"
        for u in updates
    )


# ------------------------------------------------------------------ #
# 4. Structural: the block is applied to OPEN only, and the placement
#    client is attached on a path auto-execute cannot reach
# ------------------------------------------------------------------ #

def test_the_block_is_applied_to_open_intents_only():
    import inspect

    from signal_consumer import SignalConsumer

    src = inspect.getsource(SignalConsumer._process_intent)
    # opens_blocked appears exactly twice: once as the parameter, once in the
    # single condition that consumes it — and that condition is ANDed with the
    # intent being an OPEN.
    assert src.count("opens_blocked") == 2
    assert src.count("and opens_blocked") == 1
    assert 'order["intent"] == "OPEN"' in src
    # There is no CLOSE-side counterpart anywhere in the function.
    assert 'order["intent"] == "CLOSE"' not in src


def test_auto_execute_is_not_an_input_to_trader_attachment():
    """`set_trader` decides whether a placement path exists at all. If
    auto-execute reached it, turning auto-execute off would remove the ability
    to CLOSE — the unsafe design this test forbids."""
    import inspect

    src = inspect.getsource(UserSession.run_cycle)
    attach = src[src.index("self._consumer.set_trader(") :]
    attach = attach[: attach.index(")\n")]
    assert "auto_execute" not in attach
    assert "_blocked_reason" not in attach
    assert "effective_trade_capable" in attach


def test_auto_execute_is_consulted_in_exactly_one_decision():
    """Every other reference is telemetry. A second decision path reading it
    could gate a CLOSE without this file noticing."""
    import inspect

    import live_controls

    src = inspect.getsource(live_controls.placement_block_reason)
    # Exactly one branch reads it, and it can only ever return a reason string.
    assert src.count("if auto_execute_enabled") == 1
    assert "auto_execute_enabled is not True" in src
    # The docstring states the property outright; the executable body has no
    # CLOSE branch at all, so the verdict is about the CYCLE and the caller is
    # what applies it to OPENs alone.
    assert "CLOSE is deliberately NOT governed by this" in src
    body = src.split('"""')[2]
    assert "CLOSE" not in body
    assert "intent" not in body


def test_the_risk_guard_exempts_close_from_every_open_only_rule():
    """The last gate before placement. A CLOSE needs only a matching position."""
    guard = RiskGuard(max_leverage=90)
    close = {
        "symbol": SYMBOL,
        "intent": "CLOSE",
        "side": "LONG",
        "qty": 0.5,
        "notional_usd": 0.0,   # under the min notional
        "leverage": 999,       # over the exchange maximum
    }
    import time as _time

    allowed, reason = guard.evaluate(close, 0.5, _time.time())
    assert allowed is True, reason


@pytest.mark.parametrize(
    "blocked_reason",
    ["auto_execute_disabled", "kill_switch_active", "reconcile_mismatch"],
)
def test_no_block_reason_prevents_a_close(monkeypatch, blocked_reason):
    """Whatever stops OPENs during the window, the exit stays available."""
    from signal_consumer import SignalConsumer

    c = SignalConsumer("http://app.invalid", "tok", USER, "LIVE_TRADE", SYMBOL)
    c._risk_guard = RiskGuard(max_leverage=90)
    placed: list = []
    monkeypatch.setattr(c, "_post_order", lambda order: True)
    monkeypatch.setattr(c, "_post_order_update", lambda update: None)
    monkeypatch.setattr(c, "_place_order", lambda order: placed.append(order) or 0.0)

    from decimal import Decimal

    close = c._build_close_intent(CLOSE_SIGNAL, Decimal("2500"), 0.5)
    c._binance_trader = object()  # a placement path exists
    c._process_intent(close, 0.5, opens_blocked=True, block_reason=blocked_reason)

    assert len(placed) == 1
    assert placed[0]["intent"] == "CLOSE"
    assert placed[0]["status"] == "INTENT_LOGGED"
