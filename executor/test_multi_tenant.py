"""Dynamic onboarding, and the isolation that makes it safe.

The property under test is that a client can sign up, connect keys, configure and
start — and be executed — with no operator touching a `.env`, while remaining
completely unable to affect, or be affected by, any other client.

Each test names the failure it would catch. The ones that matter most:

  * A session signing with another user's keys would be a client's funds moving
    on someone else's decision. Asserted directly, per session, per client.
  * One user's failure taking down the loop would stop every other client's
    trading — the multi-tenant version of an outage.
  * One user's missing keys blocking the loop would mean an unconfigured signup
    could halt paying clients.

Nothing here reaches a network, and no order can be created: every Binance
client is a fake that records what it was constructed with and raises if asked
to place anything.
"""

from decimal import Decimal

import pytest

import multi_tenant
import user_credentials
from multi_tenant import ActiveUserRoster, MultiTenantExecutor, RosterUnavailable
from user_credentials import CredentialsResult, UserCredentials
from user_session import FatalConfigError, HostLimits, UserSession

ALICE = "aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"
BOB = "bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb"
CAROL = "cccccccc-3333-3333-3333-cccccccccccc"

ALICE_KEY, ALICE_SECRET = "ALICEKEY_aaaaaaaaaaaaaaaa", "ALICESECRET_aaaaaaaaaaaa"
BOB_KEY, BOB_SECRET = "BOBKEY_bbbbbbbbbbbbbbbbbb", "BOBSECRET_bbbbbbbbbbbbbb"


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeBinanceClient:
    instances: list = []

    def __init__(self, base_url, api_key, api_secret):
        self.base_url = base_url
        self.api_key = api_key
        self.api_secret = api_secret
        self.clock_offset_ms = 0
        FakeBinanceClient.instances.append(self)

    def sync_clock(self):
        return 0

    def get_exchange_info(self, symbol):
        return {"filters": []}

    def get_positions(self, symbol):
        return [{"positionAmt": "0", "leverage": "1", "marginType": "isolated"}]

    def get_account(self):
        return {"totalWalletBalance": "1000", "availableBalance": "1000"}

    def get_leverage_brackets(self, symbol):
        return [{"initialLeverage": 1, "notionalCap": "1000000"}]

    def set_leverage(self, symbol, leverage):
        pass

    def set_margin_type(self, symbol, margin_type):
        pass

    def place_market_order(self, *a, **kw):  # pragma: no cover
        raise AssertionError("a test attempted to place an order")


class FakeConsumer:
    """Per-user consumer. Records the user it was built for."""

    instances: list = []
    # Per-user overrides, keyed by user id.
    modes: dict = {}
    running: dict = {}
    raises: dict = {}

    def __init__(self, base, token, user_id, execution_mode, symbol, **kwargs):
        self.user_id = user_id
        self.db_execution_mode = FakeConsumer.modes.get(user_id, "LIVE_TRADE")
        self.db_auto_execute_enabled = True
        self.db_is_running = FakeConsumer.running.get(user_id, True)
        self.live_order_cap_usd = Decimal("25")
        self.desired_leverage = 1
        self.last_reconcile = None
        self.traders: list = []
        self.polls = 0
        FakeConsumer.instances.append(self)

    def ensure_config(self):
        exc = FakeConsumer.raises.get(self.user_id)
        if exc is not None:
            raise exc

    def end_cycle(self):
        pass

    def set_trader(self, trader):
        self.traders.append(trader)

    def set_available_balance(self, balance):
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
    def __init__(self, *a, **kw):
        pass

    def update_limits(self, **kw):
        pass


class FakeReporter:
    def __init__(self):
        self.sent: list = []

    def report(self, snapshot, user_id=None):
        self.sent.append((user_id, snapshot))
        return True

    def for_user(self, user_id):
        return [snap for uid, snap in self.sent if uid == user_id]


class FakeCredentialsClient:
    """One per user, bound to that user — exactly as production builds them."""

    keys: dict = {}

    def __init__(self, user_id):
        self.user_id = user_id
        self.fetches = 0

    def fetch(self):
        self.fetches += 1
        entry = FakeCredentialsClient.keys.get(self.user_id)
        if entry is None:
            return CredentialsResult(
                blocked_reason=user_credentials.MISSING_KEYS_REASON
            )
        if isinstance(entry, BaseException):
            raise entry
        key, secret = entry
        return CredentialsResult(credentials=UserCredentials(key, secret, key[-4:]))


class FakeRoster:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = 0

    def fetch(self):
        self.calls += 1
        r = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(r, BaseException):
            raise r
        return list(r)


@pytest.fixture
def env(monkeypatch):
    """Patch the module attributes every session looks up at construction."""
    FakeBinanceClient.instances = []
    FakeConsumer.instances = []
    FakeConsumer.modes = {}
    FakeConsumer.running = {}
    FakeConsumer.raises = {}
    FakeCredentialsClient.keys = {}

    monkeypatch.setattr("binance_client.BinanceFuturesClient", FakeBinanceClient)
    monkeypatch.setattr("binance_client.ReadOnlyFuturesClient", FakeBinanceClient)
    monkeypatch.setattr("signal_consumer.SignalConsumer", FakeConsumer)
    monkeypatch.setattr("risk_guard.RiskGuard", FakeRiskGuard)
    return monkeypatch


def limits(env_mode="LIVE_TRADE"):
    return HostLimits(
        env_mode=env_mode,
        base_url="https://fapi.binance.com",
        trade_capable=env_mode == "LIVE_TRADE",
        ack_present=True,
        live_order_cap_usd=Decimal("25"),
        env_order_cap_reported=Decimal("25"),
        app_api_base="https://app.example.test",
        engine_service_token="service-token",
        is_live=True,
    )


def make_loop(reporter=None, roster=None, env_mode="LIVE_TRADE", sleeper=None):
    host = limits(env_mode)
    reporter = reporter if reporter is not None else FakeReporter()

    def factory(user_id):
        return UserSession(
            user_id=user_id,
            limits=host,
            credentials_client=FakeCredentialsClient(user_id),
            reporter=reporter,
        )

    loop = MultiTenantExecutor(
        roster=roster if roster is not None else FakeRoster([ALICE, BOB]),
        session_factory=factory,
        heartbeat_interval_seconds=0,
        sleeper=sleeper if sleeper is not None else (lambda _s: None),
    )
    return loop, reporter


# --------------------------------------------------------------------------- #
# 1. onboarding needs no operator
# --------------------------------------------------------------------------- #


def test_a_user_on_the_roster_is_executed_with_no_env_change(env):
    """The requirement, stated directly: appearing on the roster is enough."""
    FakeCredentialsClient.keys = {ALICE: (ALICE_KEY, ALICE_SECRET)}
    loop, reporter = make_loop(roster=FakeRoster([ALICE]))

    loop.run_forever(max_iterations=1)

    assert ALICE in loop.sessions
    session = loop.sessions[ALICE]
    assert session.client is not None
    assert session.client.api_key == ALICE_KEY
    assert reporter.for_user(ALICE), "the new user got no telemetry"


def test_a_user_appearing_mid_run_is_picked_up_without_a_restart(env):
    """A client signs up and enables execution while the loop is already running."""
    FakeCredentialsClient.keys = {
        ALICE: (ALICE_KEY, ALICE_SECRET),
        BOB: (BOB_KEY, BOB_SECRET),
    }
    loop, _ = make_loop(roster=FakeRoster([ALICE], [ALICE, BOB]))

    loop.run_forever(max_iterations=1)
    assert set(loop.sessions) == {ALICE}

    loop.run_forever(max_iterations=1)
    assert set(loop.sessions) == {ALICE, BOB}
    assert loop.sessions[BOB].client.api_key == BOB_KEY


def test_a_user_leaving_the_roster_is_retired_with_their_client(env):
    """Disabling execution must drop the means of signing, not just the intent."""
    FakeCredentialsClient.keys = {
        ALICE: (ALICE_KEY, ALICE_SECRET),
        BOB: (BOB_KEY, BOB_SECRET),
    }
    loop, _ = make_loop(roster=FakeRoster([ALICE, BOB], [ALICE]))

    loop.run_forever(max_iterations=1)
    assert set(loop.sessions) == {ALICE, BOB}

    loop.run_forever(max_iterations=1)
    assert set(loop.sessions) == {ALICE}


def test_an_empty_roster_is_survivable(env):
    loop, _ = make_loop(roster=FakeRoster([]))
    assert loop.run_forever(max_iterations=1) == 0
    assert loop.sessions == {}


def test_a_roster_outage_keeps_existing_sessions_running(env):
    """Emptying the roster on a network blip would stop trading for everyone."""
    FakeCredentialsClient.keys = {ALICE: (ALICE_KEY, ALICE_SECRET)}
    loop, _ = make_loop(
        roster=FakeRoster([ALICE], RosterUnavailable("app unreachable"))
    )

    loop.run_forever(max_iterations=1)
    assert set(loop.sessions) == {ALICE}

    loop.run_forever(max_iterations=1)
    assert set(loop.sessions) == {ALICE}, "a roster blip retired a live session"


# --------------------------------------------------------------------------- #
# 2. no cross-client wallet use
# --------------------------------------------------------------------------- #


def test_each_session_signs_only_with_its_own_users_keys(env):
    """The isolation property. A breach here moves a client's real money."""
    FakeCredentialsClient.keys = {
        ALICE: (ALICE_KEY, ALICE_SECRET),
        BOB: (BOB_KEY, BOB_SECRET),
    }
    loop, _ = make_loop(roster=FakeRoster([ALICE, BOB]))

    loop.run_forever(max_iterations=1)

    alice = loop.sessions[ALICE]
    bob = loop.sessions[BOB]
    assert (alice.client.api_key, alice.client.api_secret) == (ALICE_KEY, ALICE_SECRET)
    assert (bob.client.api_key, bob.client.api_secret) == (BOB_KEY, BOB_SECRET)
    # Not the same object, and not the same credentials.
    assert alice.client is not bob.client
    assert alice.client.api_key != bob.client.api_key


def test_sessions_share_no_consumer_and_no_cursor(env):
    """A shared consumer would let one client's position drive another's sizing."""
    FakeCredentialsClient.keys = {
        ALICE: (ALICE_KEY, ALICE_SECRET),
        BOB: (BOB_KEY, BOB_SECRET),
    }
    loop, _ = make_loop(roster=FakeRoster([ALICE, BOB]))
    loop.run_forever(max_iterations=1)

    alice = loop.sessions[ALICE]
    bob = loop.sessions[BOB]
    assert alice.consumer is not bob.consumer
    assert alice.consumer.user_id == ALICE
    assert bob.consumer.user_id == BOB


def test_a_credentials_client_is_bound_to_one_user(env):
    """Production builds one credentials client per user id. A shared one could
    fetch the wrong account's keys."""
    FakeCredentialsClient.keys = {
        ALICE: (ALICE_KEY, ALICE_SECRET),
        BOB: (BOB_KEY, BOB_SECRET),
    }
    loop, _ = make_loop(roster=FakeRoster([ALICE, BOB]))
    loop.run_forever(max_iterations=1)

    assert loop.sessions[ALICE]._credentials_client.user_id == ALICE
    assert loop.sessions[BOB]._credentials_client.user_id == BOB


def test_telemetry_is_attributed_per_user(env):
    """A misattributed snapshot shows one client another client's balance."""
    FakeCredentialsClient.keys = {
        ALICE: (ALICE_KEY, ALICE_SECRET),
        BOB: (BOB_KEY, BOB_SECRET),
    }
    loop, reporter = make_loop(roster=FakeRoster([ALICE, BOB]))
    loop.run_forever(max_iterations=1)

    assert reporter.for_user(ALICE)
    assert reporter.for_user(BOB)
    assert all(uid in (ALICE, BOB) for uid, _ in reporter.sent)
    assert all(uid is not None for uid, _ in reporter.sent)


def test_no_key_material_reaches_telemetry_in_a_multi_user_loop(env):
    FakeCredentialsClient.keys = {
        ALICE: (ALICE_KEY, ALICE_SECRET),
        BOB: (BOB_KEY, BOB_SECRET),
    }
    loop, reporter = make_loop(roster=FakeRoster([ALICE, BOB]))
    loop.run_forever(max_iterations=1)

    blob = repr(reporter.sent)
    for secret in (ALICE_KEY, ALICE_SECRET, BOB_KEY, BOB_SECRET):
        assert secret not in blob


def test_a_session_cannot_be_built_without_a_user(env):
    with pytest.raises(ValueError):
        UserSession(user_id="", limits=limits())


# --------------------------------------------------------------------------- #
# 3. one client's problem stays that client's problem
# --------------------------------------------------------------------------- #


def test_missing_keys_for_one_user_do_not_block_another(env):
    """An unconfigured signup must not halt a paying client."""
    FakeCredentialsClient.keys = {BOB: (BOB_KEY, BOB_SECRET)}  # Alice has none
    loop, reporter = make_loop(roster=FakeRoster([ALICE, BOB]))

    loop.run_forever(max_iterations=1)

    alice = loop.sessions[ALICE]
    assert alice.client is None
    assert alice.orders_enabled is False
    assert alice.blocked_reason == "missing_user_binance_keys"
    assert reporter.for_user(ALICE)[-1]["keys_present"] is False

    bob = loop.sessions[BOB]
    assert bob.client is not None
    assert bob.client.api_key == BOB_KEY
    assert bob.consumer.polls == 1, "Bob's cycle did not complete"


def test_a_failing_user_does_not_end_the_loop_or_the_others(env):
    from signal_consumer import SignalConsumerError

    FakeCredentialsClient.keys = {
        ALICE: (ALICE_KEY, ALICE_SECRET),
        BOB: (BOB_KEY, BOB_SECRET),
    }
    FakeConsumer.raises = {ALICE: SignalConsumerError("alice's config is broken")}
    loop, reporter = make_loop(roster=FakeRoster([ALICE, BOB]))

    loop.run_forever(max_iterations=1)

    assert loop.sessions[ALICE].consecutive_failures == 1
    assert loop.sessions[BOB].consecutive_failures == 0
    assert loop.sessions[BOB].consumer.polls == 1
    assert "cycle failed" in (reporter.for_user(ALICE)[-1]["message"] or "")


def test_an_unexpected_error_in_one_user_is_contained(env):
    """The catch-all exists so an unforeseen bug degrades one client rather than
    taking down a process executing other people's money."""
    FakeCredentialsClient.keys = {
        ALICE: (ALICE_KEY, ALICE_SECRET),
        BOB: (BOB_KEY, BOB_SECRET),
    }
    FakeConsumer.raises = {ALICE: RuntimeError("something nobody predicted")}
    loop, _ = make_loop(roster=FakeRoster([ALICE, BOB]))

    loop.run_forever(max_iterations=1)  # must not raise

    assert loop.sessions[ALICE].consecutive_failures == 1
    assert loop.sessions[BOB].consumer.polls == 1


def test_a_user_is_parked_after_exhausting_their_budget_and_others_continue(env):
    from signal_consumer import SignalConsumerError

    FakeCredentialsClient.keys = {
        ALICE: (ALICE_KEY, ALICE_SECRET),
        BOB: (BOB_KEY, BOB_SECRET),
    }
    FakeConsumer.raises = {ALICE: SignalConsumerError("persistent")}
    loop, _ = make_loop(roster=FakeRoster([ALICE, BOB]))

    loop.run_forever(max_iterations=multi_tenant.MAX_CONSECUTIVE_FAILURES)

    assert ALICE in loop.parked
    assert ALICE not in loop.sessions
    # Bob kept trading throughout.
    assert BOB in loop.sessions
    assert loop.sessions[BOB].consumer.polls == multi_tenant.MAX_CONSECUTIVE_FAILURES


def test_a_fatal_config_error_parks_one_user_rather_than_the_process(env):
    FakeCredentialsClient.keys = {
        ALICE: (ALICE_KEY, ALICE_SECRET),
        BOB: (BOB_KEY, BOB_SECRET),
    }
    FakeConsumer.raises = {ALICE: FatalConfigError("isolated margin refused")}
    loop, _ = make_loop(roster=FakeRoster([ALICE, BOB]))

    loop.run_forever(max_iterations=1)

    assert ALICE in loop.parked
    assert BOB in loop.sessions
    assert loop.sessions[BOB].consumer.polls == 1


def test_a_session_that_cannot_be_constructed_does_not_stop_the_others(env):
    FakeCredentialsClient.keys = {BOB: (BOB_KEY, BOB_SECRET)}

    def factory(user_id):
        if user_id == ALICE:
            raise RuntimeError("alice's session could not be built")
        return UserSession(
            user_id=user_id,
            limits=limits(),
            credentials_client=FakeCredentialsClient(user_id),
            reporter=FakeReporter(),
        )

    loop = MultiTenantExecutor(
        roster=FakeRoster([ALICE, BOB]),
        session_factory=factory,
        heartbeat_interval_seconds=0,
        sleeper=lambda _s: None,
    )
    loop.run_forever(max_iterations=1)

    assert ALICE not in loop.sessions
    assert BOB in loop.sessions


# --------------------------------------------------------------------------- #
# 4. per-user controls stay per-user
# --------------------------------------------------------------------------- #


def test_stopping_one_user_does_not_affect_another(env):
    """The per-user kill switch. Alice presses Stop; Bob keeps trading."""
    FakeCredentialsClient.keys = {
        ALICE: (ALICE_KEY, ALICE_SECRET),
        BOB: (BOB_KEY, BOB_SECRET),
    }
    FakeConsumer.running = {ALICE: False, BOB: True}
    loop, _ = make_loop(roster=FakeRoster([ALICE, BOB]))

    loop.run_forever(max_iterations=1)

    assert loop.sessions[ALICE].orders_enabled is False
    assert loop.sessions[ALICE].blocked_reason == "kill_switch_active"
    assert loop.sessions[BOB].orders_enabled is True
    assert loop.sessions[BOB].blocked_reason is None


def test_one_user_at_off_does_not_silence_another(env):
    FakeCredentialsClient.keys = {
        ALICE: (ALICE_KEY, ALICE_SECRET),
        BOB: (BOB_KEY, BOB_SECRET),
    }
    FakeConsumer.modes = {ALICE: "OFF", BOB: "LIVE_TRADE"}
    loop, _ = make_loop(roster=FakeRoster([ALICE, BOB]))

    loop.run_forever(max_iterations=1)

    assert loop.sessions[ALICE].effective_mode == "OFF"
    # Effective OFF touches no exchange, so it never asks for that user's keys.
    assert loop.sessions[ALICE]._credentials_client.fetches == 0
    assert loop.sessions[BOB].effective_mode == "LIVE_TRADE"
    assert loop.sessions[BOB].orders_enabled is True


def test_a_read_only_host_gives_no_user_a_trader(env):
    """The host .env remains the ceiling for every user on it."""
    FakeCredentialsClient.keys = {
        ALICE: (ALICE_KEY, ALICE_SECRET),
        BOB: (BOB_KEY, BOB_SECRET),
    }
    loop, _ = make_loop(roster=FakeRoster([ALICE, BOB]), env_mode="LIVE_READ")

    loop.run_forever(max_iterations=1)

    for user in (ALICE, BOB):
        session = loop.sessions[user]
        assert session.effective_mode == "LIVE_READ"
        assert session.orders_enabled is False
        assert all(t is None for t in session.consumer.traders)


# --------------------------------------------------------------------------- #
# 5. roster parsing
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

    def get(self, url, timeout=None):
        self.calls.append(url)
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


def roster_with(response):
    roster = ActiveUserRoster("https://app.example.test/", "service-token")
    roster._session = FakeSession(response)
    return roster


def test_roster_reads_the_user_list():
    roster = roster_with(
        FakeResponse(200, {"users": [{"user_id": ALICE}, {"user_id": BOB}]})
    )
    assert roster.fetch() == [ALICE, BOB]


def test_roster_accepts_a_bare_list():
    roster = roster_with(FakeResponse(200, [ALICE, BOB]))
    assert roster.fetch() == [ALICE, BOB]


def test_roster_drops_duplicates():
    """Two sessions for one account would each be unaware of the other's
    position, and both could open against it."""
    roster = roster_with(
        FakeResponse(200, {"users": [{"user_id": ALICE}, {"user_id": ALICE}]})
    )
    assert roster.fetch() == [ALICE]


def test_roster_ignores_unusable_entries():
    roster = roster_with(
        FakeResponse(200, {"users": [{"user_id": ALICE}, {"user_id": ""}, {}, None, 7]})
    )
    assert roster.fetch() == [ALICE]


@pytest.mark.parametrize("status", [401, 403, 500, 404])
def test_roster_failures_raise_rather_than_returning_empty(status):
    """Returning [] on an error would retire every session at once."""
    roster = roster_with(FakeResponse(status, {"error": "nope"}))
    with pytest.raises(RosterUnavailable):
        roster.fetch()


def test_a_roster_transport_failure_raises():
    roster = roster_with(OSError("connection refused"))
    with pytest.raises(RosterUnavailable):
        roster.fetch()


def test_a_non_json_roster_raises():
    roster = roster_with(FakeResponse(200, raises=True))
    with pytest.raises(RosterUnavailable):
        roster.fetch()


def test_a_roster_without_a_list_raises():
    roster = roster_with(FakeResponse(200, {"count": 2}))
    with pytest.raises(RosterUnavailable):
        roster.fetch()


def test_roster_url_has_no_double_slash():
    roster = roster_with(FakeResponse(200, {"users": []}))
    roster.fetch()
    assert roster._session.calls[0].endswith("/api/public/engine/users/active")
    assert "//api" not in roster._session.calls[0].replace("https://", "")
