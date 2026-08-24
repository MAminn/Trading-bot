"""The connected user's Binance keys reach the executor — and nothing else does.

Covers the production blocker directly: a client could enter their Binance keys
on the website while the executor kept signing mainnet orders with the server
operator's `.env` keys. These tests assert the three properties that fix means:

  1. LIVE modes sign with the USER's keys, and the legacy `.env` live keys are
     not consulted even when they are set to something perfectly usable.
  2. LIVE modes fail CLOSED when the user has connected no keys: no client is
     built, no signed call is made, orders_enabled is False, and the telemetry
     carries a blocked_reason that names the cause.
  3. No key material appears in any telemetry payload or log line, ever.

Nothing here reaches a network. Every Binance client is a fake that records
what it was constructed with, so "which key would have signed" is observable
without a single request leaving the process — and no order, live or otherwise,
can be created by this file.
"""

import logging
import types
from decimal import Decimal

import pytest

import main
import user_credentials
from user_credentials import (
    CredentialsResult,
    CredentialsUnavailable,
    UserCredentials,
    UserCredentialsClient,
)

# Distinctive, obviously fake, and long enough that a partial leak still matches.
USER_KEY = "USERKEY_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1234"
USER_SECRET = "USERSECRET_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
ROTATED_KEY = "USERKEY_cccccccccccccccccccccccccccccc9876"
ROTATED_SECRET = "USERSECRET_dddddddddddddddddddddddddddddddd"
# What the server operator used to trade with. Must never be selected again.
ENV_LIVE_KEY = "ENVLIVEKEY_eeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
ENV_LIVE_SECRET = "ENVLIVESECRET_ffffffffffffffffffffffffffff"

SECRET_STRINGS = (USER_KEY, USER_SECRET, ROTATED_KEY, ROTATED_SECRET)

PINNED_USER = "11111111-2222-3333-4444-555555555555"


class LoopBreak(BaseException):
    """Ends run_executor's infinite loop from inside time.sleep.

    BaseException on purpose: run_executor catches several concrete exception
    groups, and a test sentinel must pass straight through all of them rather
    than being absorbed and retried.
    """


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeBinanceClient:
    """Records the credentials it was handed. Makes no network call."""

    instances: list = []

    def __init__(self, base_url, api_key, api_secret):
        self.base_url = base_url
        self.api_key = api_key
        self.api_secret = api_secret
        self.clock_offset_ms = 0
        self.write_calls: list = []
        FakeBinanceClient.instances.append(self)

    # --- reads --- #
    def sync_clock(self):
        return 0

    def get_exchange_info(self, symbol):
        return {"filters": [], "pricePrecision": 2, "quantityPrecision": 3}

    def get_positions(self, symbol):
        return [
            {
                "positionAmt": "0",
                "entryPrice": "0",
                "leverage": "1",
                "marginType": "isolated",
            }
        ]

    def get_account(self):
        return {"totalWalletBalance": "1000", "availableBalance": "1000"}

    def get_leverage_brackets(self, symbol):
        return [{"initialLeverage": 1, "notionalCap": "1000000"}]

    # --- writes: recorded, never performed --- #
    def set_leverage(self, symbol, leverage):
        self.write_calls.append(("set_leverage", symbol, leverage))

    def set_margin_type(self, symbol, margin_type):
        self.write_calls.append(("set_margin_type", symbol, margin_type))

    def place_market_order(self, *a, **kw):  # pragma: no cover - must never run
        raise AssertionError("a test attempted to place an order")


class FakeConsumer:
    """Stands in for SignalConsumer: records the trader it is given."""

    db_mode = "LIVE_TRADE"

    def __init__(self, *args, **kwargs):
        self.db_execution_mode = FakeConsumer.db_mode
        self.db_auto_execute_enabled = True
        self.db_is_running = True
        self.live_order_cap_usd = Decimal("25")
        self.desired_leverage = 1
        self.last_reconcile = None
        self.traders: list = []
        self.polls = 0

    def ensure_config(self):
        pass

    def end_cycle(self):
        pass

    def set_trader(self, trader):
        self.traders.append(trader)

    def set_account_balances(self, *, wallet_balance, available_balance):
        pass

    def set_leverage_limits(self, max_leverage, ladder):
        pass

    def reconcile(self, position_amt):
        return False, None

    def poll_once(self, **kwargs):
        self.polls += 1

    def run_smoke_test(self, side, position_amt):  # pragma: no cover
        raise AssertionError("smoke test must not run here")


class FakeRiskGuard:
    """Real caps are exercised in test_risk_guard.py; here it only has to exist."""

    def __init__(self, *a, **kw):
        self.limits = kw

    def update_limits(self, **kw):
        self.limits.update(kw)


class FakeReporter:
    def __init__(self):
        self.snapshots: list = []
        # Who each snapshot was attributed to. In a multi-tenant loop one
        # reporter serves every session, so misattribution would show a client
        # someone else's balance — the pairing is worth recording.
        self.addressed: list = []

    def report(self, snapshot, user_id=None):
        self.snapshots.append(snapshot)
        self.addressed.append(user_id)
        return True


class FakeCredentialsClient:
    """Returns a scripted sequence of credential answers."""

    def __init__(self, *results):
        self._results = list(results)
        self.fetches = 0

    def fetch(self):
        self.fetches += 1
        result = self._results[min(self.fetches - 1, len(self._results) - 1)]
        if isinstance(result, BaseException):
            raise result
        return result


def present(key=USER_KEY, secret=USER_SECRET, last4=None):
    return CredentialsResult(
        credentials=UserCredentials(key, secret, last4 or key[-4:])
    )


def blocked(reason=user_credentials.MISSING_KEYS_REASON):
    return CredentialsResult(blocked_reason=reason)


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #


@pytest.fixture
def harness(monkeypatch):
    """Runs run_executor for a bounded number of cycles against fakes."""
    FakeBinanceClient.instances = []
    FakeConsumer.db_mode = "LIVE_TRADE"
    reporter = FakeReporter()

    monkeypatch.setattr("binance_client.BinanceFuturesClient", FakeBinanceClient)
    monkeypatch.setattr("binance_client.ReadOnlyFuturesClient", FakeBinanceClient)
    monkeypatch.setattr("signal_consumer.SignalConsumer", FakeConsumer)
    monkeypatch.setattr("risk_guard.RiskGuard", FakeRiskGuard)
    monkeypatch.setattr(main, "build_reporter", lambda **kwargs: reporter)

    # The env every non-OFF mode validates before it does anything else.
    monkeypatch.setenv("APP_API_BASE", "https://app.example.test")
    monkeypatch.setenv("ENGINE_SERVICE_TOKEN", "service-token")
    monkeypatch.setenv("ENGINE_CREDENTIALS_TOKEN", "credentials-token")
    # Pinned: every test in this file is about ONE user's credentials. The
    # multi-tenant loop has its own suite.
    monkeypatch.setenv("ENGINE_USER_ID", PINNED_USER)
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND_REAL_MONEY")
    monkeypatch.setenv("LIVE_ORDER_CAP_USD", "25")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "testnet-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "testnet-secret")
    monkeypatch.delenv("LIVE_SMOKE_TEST", raising=False)
    monkeypatch.delenv("CONSUMER_START_AFTER", raising=False)

    def run(mode, credentials_client, cycles=1):
        monkeypatch.setattr(
            "user_credentials.UserCredentialsClient",
            lambda *a, **kw: credentials_client,
        )
        remaining = {"n": cycles}

        def fake_sleep(_seconds):
            remaining["n"] -= 1
            if remaining["n"] <= 0:
                raise LoopBreak
            return None

        monkeypatch.setattr(main, "time", types.SimpleNamespace(sleep=fake_sleep))
        with pytest.raises(LoopBreak):
            main.run_executor(mode)
        return reporter

    run.reporter = reporter
    return run


# --------------------------------------------------------------------------- #
# 1. the user's keys are the ones that sign
# --------------------------------------------------------------------------- #


def test_live_trade_signs_with_the_connected_users_keys(harness, monkeypatch):
    monkeypatch.setenv("BINANCE_LIVE_API_KEY", ENV_LIVE_KEY)
    monkeypatch.setenv("BINANCE_LIVE_API_SECRET", ENV_LIVE_SECRET)

    harness("LIVE_TRADE", FakeCredentialsClient(present()))

    assert FakeBinanceClient.instances, "no Binance client was ever built"
    client = FakeBinanceClient.instances[-1]
    assert client.api_key == USER_KEY
    assert client.api_secret == USER_SECRET
    # The env pair was set to something perfectly usable and still lost.
    assert client.api_key != ENV_LIVE_KEY
    assert client.api_secret != ENV_LIVE_SECRET
    assert client.base_url == main.LIVE_BASE_URL


def test_live_read_signs_with_the_connected_users_keys(harness, monkeypatch):
    monkeypatch.setenv("BINANCE_LIVE_API_KEY", ENV_LIVE_KEY)
    monkeypatch.setenv("BINANCE_LIVE_API_SECRET", ENV_LIVE_SECRET)
    FakeConsumer.db_mode = "LIVE_READ"

    harness("LIVE_READ", FakeCredentialsClient(present()))

    client = FakeBinanceClient.instances[-1]
    assert (client.api_key, client.api_secret) == (USER_KEY, USER_SECRET)


def test_env_live_keys_alone_cannot_start_a_live_client(harness, monkeypatch):
    """The blocker, stated as a test: env keys present, user keys absent, and
    the result must be no client at all rather than a fallback."""
    monkeypatch.setenv("BINANCE_LIVE_API_KEY", ENV_LIVE_KEY)
    monkeypatch.setenv("BINANCE_LIVE_API_SECRET", ENV_LIVE_SECRET)

    harness("LIVE_TRADE", FakeCredentialsClient(blocked()))

    assert FakeBinanceClient.instances == []


def test_rotated_keys_are_picked_up_without_a_restart(harness):
    reporter = harness(
        "LIVE_TRADE",
        FakeCredentialsClient(present(), present(ROTATED_KEY, ROTATED_SECRET)),
        cycles=2,
    )
    assert len(FakeBinanceClient.instances) == 2
    assert FakeBinanceClient.instances[0].api_key == USER_KEY
    assert FakeBinanceClient.instances[1].api_key == ROTATED_KEY
    assert reporter.snapshots


def test_unchanged_keys_do_not_rebuild_the_client(harness):
    harness("LIVE_TRADE", FakeCredentialsClient(present(), present()), cycles=3)
    assert len(FakeBinanceClient.instances) == 1


def test_testnet_still_uses_env_keys_and_never_asks_the_app(harness):
    """Requirement 3: the testnet path is untouched."""
    credentials = FakeCredentialsClient(present())

    harness("TESTNET_TRADE", credentials)

    client = FakeBinanceClient.instances[-1]
    assert (client.api_key, client.api_secret) == ("testnet-key", "testnet-secret")
    assert client.base_url == main.TESTNET_BASE_URL
    # A testnet host has no business asking the app for a user's mainnet keys.
    assert credentials.fetches == 0


# --------------------------------------------------------------------------- #
# 2. fail closed when the user has connected nothing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["LIVE_READ", "LIVE_TRADE"])
def test_missing_user_keys_block_every_live_mode(harness, mode):
    FakeConsumer.db_mode = mode
    reporter = harness(mode, FakeCredentialsClient(blocked()))

    assert FakeBinanceClient.instances == [], "a live client was built with no keys"
    snapshot = reporter.snapshots[-1]
    assert snapshot["orders_enabled"] is False
    assert snapshot["blocked_reason"] == "missing_user_binance_keys"
    assert snapshot["keys_present"] is False


def test_blocked_cycle_makes_no_signed_call_and_reads_no_balance(harness):
    reporter = harness("LIVE_TRADE", FakeCredentialsClient(blocked()))
    snapshot = reporter.snapshots[-1]
    # Nothing was read, so nothing is reported — not a stale or invented zero.
    assert snapshot["wallet_balance_usd"] is None
    assert snapshot["available_balance_usd"] is None
    assert snapshot["position_side"] is None


def test_blocked_cycle_leaves_the_consumer_with_no_trader(harness):
    """Placement is removed, not flagged: the consumer reaches its placement
    path only through a non-None trader."""
    consumers: list = []
    original_init = FakeConsumer.__init__

    def recording_init(self, *a, **kw):
        original_init(self, *a, **kw)
        consumers.append(self)

    FakeConsumer.__init__ = recording_init
    try:
        harness("LIVE_TRADE", FakeCredentialsClient(blocked()))
    finally:
        FakeConsumer.__init__ = original_init

    assert consumers, "no consumer was constructed"
    assert consumers[-1].traders, "set_trader was never called"
    assert all(t is None for t in consumers[-1].traders)


def test_undecryptable_keys_are_reported_distinctly(harness):
    """A row that exists but cannot be decrypted is an operator problem, and
    must not be reported as a user who has not connected."""
    reporter = harness(
        "LIVE_TRADE",
        FakeCredentialsClient(blocked(user_credentials.UNDECRYPTABLE_REASON)),
    )
    snapshot = reporter.snapshots[-1]
    assert snapshot["blocked_reason"] == "user_binance_keys_undecryptable"
    assert snapshot["orders_enabled"] is False
    assert FakeBinanceClient.instances == []


def test_keys_removed_mid_run_drop_the_client(harness):
    """A client disconnects their account while the executor is running."""
    reporter = harness(
        "LIVE_TRADE", FakeCredentialsClient(present(), blocked()), cycles=2
    )
    assert len(FakeBinanceClient.instances) == 1
    last = reporter.snapshots[-1]
    assert last["orders_enabled"] is False
    assert last["blocked_reason"] == "missing_user_binance_keys"
    assert last["keys_present"] is False


def test_a_credentials_outage_fails_the_cycle_rather_than_unblocking(harness):
    """Transport failure must never be read as 'no keys' OR as 'keys fine'."""
    reporter = harness(
        "LIVE_TRADE",
        FakeCredentialsClient(CredentialsUnavailable("app unreachable")),
    )
    assert FakeBinanceClient.instances == []
    assert "cycle failed" in (reporter.snapshots[-1]["message"] or "")


def test_missing_credentials_token_refuses_to_start(monkeypatch):
    monkeypatch.setenv("APP_API_BASE", "https://app.example.test")
    monkeypatch.setenv("ENGINE_SERVICE_TOKEN", "service-token")
    monkeypatch.setenv("ENGINE_USER_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND_REAL_MONEY")
    monkeypatch.setenv("LIVE_ORDER_CAP_USD", "25")
    monkeypatch.delenv("ENGINE_CREDENTIALS_TOKEN", raising=False)
    assert main.run_executor("LIVE_TRADE") == 1


def test_credentials_token_equal_to_service_token_refuses_to_start(monkeypatch):
    """The separation is the security property, so sharing the token is fatal
    rather than merely discouraged."""
    monkeypatch.setenv("APP_API_BASE", "https://app.example.test")
    monkeypatch.setenv("ENGINE_SERVICE_TOKEN", "same-token")
    monkeypatch.setenv("ENGINE_CREDENTIALS_TOKEN", "same-token")
    monkeypatch.setenv("ENGINE_USER_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND_REAL_MONEY")
    monkeypatch.setenv("LIVE_ORDER_CAP_USD", "25")
    assert main.run_executor("LIVE_TRADE") == 1


# --------------------------------------------------------------------------- #
# 3. existing live gates are untouched
# --------------------------------------------------------------------------- #


def test_present_keys_do_not_bypass_the_auto_execute_gate(harness):
    """Connecting keys grants access to the account, not permission to trade."""
    original_init = FakeConsumer.__init__

    def init_with_auto_execute_off(self, *a, **kw):
        original_init(self, *a, **kw)
        self.db_auto_execute_enabled = False

    FakeConsumer.__init__ = init_with_auto_execute_off
    try:
        reporter = harness("LIVE_TRADE", FakeCredentialsClient(present()))
    finally:
        FakeConsumer.__init__ = original_init

    snapshot = reporter.snapshots[-1]
    assert snapshot["orders_enabled"] is False
    assert snapshot["blocked_reason"] == "auto_execute_disabled"


def test_present_keys_do_not_bypass_the_kill_switch(harness):
    original_init = FakeConsumer.__init__

    def init_stopped(self, *a, **kw):
        original_init(self, *a, **kw)
        self.db_is_running = False

    FakeConsumer.__init__ = init_stopped
    try:
        reporter = harness("LIVE_TRADE", FakeCredentialsClient(present()))
    finally:
        FakeConsumer.__init__ = original_init

    assert reporter.snapshots[-1]["blocked_reason"] == "kill_switch_active"


def test_present_keys_do_not_bypass_the_db_execution_mode_gate(harness):
    FakeConsumer.db_mode = "OFF"
    credentials = FakeCredentialsClient(present())
    reporter = harness("LIVE_TRADE", credentials)

    assert reporter.snapshots[-1]["orders_enabled"] is False
    # Effective OFF makes no exchange call, so it needs no key and asks for none.
    assert credentials.fetches == 0
    assert FakeBinanceClient.instances == []


def test_live_trade_without_the_ack_still_refuses_even_with_user_keys(monkeypatch):
    monkeypatch.setenv("APP_API_BASE", "https://app.example.test")
    monkeypatch.setenv("ENGINE_SERVICE_TOKEN", "service-token")
    monkeypatch.setenv("ENGINE_CREDENTIALS_TOKEN", "credentials-token")
    monkeypatch.setenv("ENGINE_USER_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("LIVE_ORDER_CAP_USD", "25")
    monkeypatch.delenv("LIVE_TRADING_ACK", raising=False)
    assert main.run_executor("LIVE_TRADE") == 1


# --------------------------------------------------------------------------- #
# 4. no key material leaves the process
# --------------------------------------------------------------------------- #


def test_no_key_material_in_telemetry_or_logs(harness, caplog):
    caplog.set_level(logging.DEBUG)
    reporter = harness(
        "LIVE_TRADE",
        FakeCredentialsClient(present(), present(ROTATED_KEY, ROTATED_SECRET)),
        cycles=2,
    )

    payloads = repr(reporter.snapshots)
    logs = caplog.text
    for secret in SECRET_STRINGS:
        assert secret not in payloads, "key material reached a telemetry payload"
        assert secret not in logs, "key material reached a log line"

    # last4 is public — it is already shown in the website UI — and its presence
    # in the log is what makes "which key am I using" answerable at all.
    assert USER_KEY[-4:] in logs


def test_credentials_repr_never_exposes_the_pair():
    creds = UserCredentials(USER_KEY, USER_SECRET, USER_KEY[-4:])
    for rendered in (repr(creds), str(creds), "{}".format(creds), "%s" % (creds,)):
        assert USER_KEY not in rendered
        assert USER_SECRET not in rendered
        assert USER_KEY[-4:] in rendered

    result = CredentialsResult(credentials=creds)
    assert USER_SECRET not in repr(result)
    assert USER_SECRET not in str(result)


def test_every_snapshot_names_the_user_it_describes(harness):
    reporter = harness("LIVE_TRADE", FakeCredentialsClient(present()))
    assert reporter.addressed, "no snapshot was reported"
    # A snapshot posted without a user, or with the wrong one, would show a
    # client another client's balance and position.
    assert all(u == PINNED_USER for u in reporter.addressed)


def test_blocked_result_repr_is_safe():
    result = blocked()
    assert "missing_user_binance_keys" in repr(result)


# --------------------------------------------------------------------------- #
# 5. UserCredentialsClient response handling
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, status_code, payload=None, raises=False):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.headers = {}
        self.calls: list = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


def build_client(response):
    client = UserCredentialsClient(
        "https://app.example.test/", "credentials-token", "user-id"
    )
    session = FakeSession(response)
    client._session = session
    return client, session


def test_client_returns_the_pair_on_200():
    client, session = build_client(
        FakeResponse(
            200,
            {
                "api_key": USER_KEY,
                "api_secret": USER_SECRET,
                "api_key_last4": USER_KEY[-4:],
            },
        )
    )
    result = client.fetch()
    assert result.present
    assert result.credentials.api_key == USER_KEY
    assert result.credentials.api_secret == USER_SECRET
    assert session.calls[0]["url"].endswith("/api/public/engine/credentials")
    assert session.calls[0]["params"] == {"user_id": "user-id"}
    # A trailing slash on the base must not produce a double slash.
    assert "//api" not in session.calls[0]["url"].replace("https://", "")


@pytest.mark.parametrize(
    "status,reason",
    [
        (404, user_credentials.MISSING_KEYS_REASON),
        (409, user_credentials.UNDECRYPTABLE_REASON),
        (503, user_credentials.NOT_CONFIGURED_REASON),
    ],
)
def test_client_maps_each_refusal_to_its_own_reason(status, reason):
    client, _ = build_client(FakeResponse(status, {"error": reason}))
    result = client.fetch()
    assert not result.present
    assert result.blocked_reason == reason


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_raises_rather_than_reporting_no_keys(status):
    """An operator's token mistake must not masquerade as a user who has not
    connected — that sends someone hunting in entirely the wrong place."""
    client, _ = build_client(FakeResponse(status, {"error": "unauthorized"}))
    with pytest.raises(CredentialsUnavailable):
        client.fetch()


def test_a_server_error_raises():
    client, _ = build_client(FakeResponse(500, {"error": "boom"}))
    with pytest.raises(CredentialsUnavailable):
        client.fetch()


def test_a_transport_failure_raises():
    client, _ = build_client(OSError("connection refused"))
    with pytest.raises(CredentialsUnavailable):
        client.fetch()


def test_a_non_json_body_raises_without_echoing_it():
    client, _ = build_client(FakeResponse(200, raises=True))
    with pytest.raises(CredentialsUnavailable) as excinfo:
        client.fetch()
    # The body of THIS endpoint is the secret, so it is never in the message.
    assert "not json" not in str(excinfo.value)


def test_an_empty_pair_fails_closed_rather_than_signing_with_blanks():
    client, _ = build_client(FakeResponse(200, {"api_key": "", "api_secret": ""}))
    result = client.fetch()
    assert not result.present
    assert result.blocked_reason == user_credentials.MISSING_KEYS_REASON


def test_last4_falls_back_to_the_key_when_absent():
    client, _ = build_client(
        FakeResponse(200, {"api_key": USER_KEY, "api_secret": USER_SECRET})
    )
    assert client.fetch().credentials.last4 == USER_KEY[-4:]
