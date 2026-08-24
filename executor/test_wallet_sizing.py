"""End-to-end wallet sizing through a real UserSession.

Where test_sizing.py pins the arithmetic, this file pins the plumbing: that the
capital base reaching the sizing formula is `totalWalletBalance` from a signed
read of THIS user's account, that two users on the same host never see each
other's wallet, and that an already-open position defers a leverage change
without ever standing between that position and the order that closes it.

Nothing here reaches the network. The Binance client is a fake that records
every write, and it raises if a test ever causes an order to be placed.
"""

from decimal import Decimal

import pytest

import user_credentials
from user_credentials import CredentialsResult, UserCredentials
from user_session import FatalConfigError, HostLimits, UserSession

SYMBOL = "ETHUSDT"
ALICE = "aaaaaaaa-1111-1111-1111-111111111111"
BOB = "bbbbbbbb-2222-2222-2222-222222222222"

BASE_CONFIG = {
    "leverage": 30,
    "capital_allocation_pct": 20,
    "execution_mode": "LIVE_TRADE",
    "auto_execute_enabled": True,
    "is_running": True,
}


class FakeClient:
    """One Binance account. Signed reads return THIS account's figures."""

    def __init__(
        self,
        base_url,
        api_key,
        api_secret,
        *,
        wallet="300",
        available=None,
        position_amt="0",
        leverage="30",
        margin_type="isolated",
        set_leverage_error=None,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.api_secret = api_secret
        self.clock_offset_ms = 0
        self.wallet = wallet
        self.available = wallet if available is None else available
        self.position_amt = position_amt
        self.leverage = leverage
        self.margin_type = margin_type
        self.set_leverage_error = set_leverage_error
        self.writes: list = []

    def sync_clock(self):
        return 0

    def get_exchange_info(self, symbol):
        return {"filters": []}

    def get_account(self):
        return {
            "totalWalletBalance": self.wallet,
            "availableBalance": self.available,
        }

    def get_positions(self, symbol):
        return [
            {
                "symbol": symbol,
                "positionAmt": self.position_amt,
                "leverage": self.leverage,
                "marginType": self.margin_type,
            }
        ]

    def get_leverage_brackets(self, symbol):
        return [
            {"initialLeverage": 90, "notionalCap": "50000"},
            {"initialLeverage": 10, "notionalCap": "5000000"},
        ]

    def set_leverage(self, symbol, leverage):
        self.writes.append(("set_leverage", leverage))
        if self.set_leverage_error is not None:
            raise self.set_leverage_error
        self.leverage = str(leverage)

    def set_margin_type(self, symbol, margin_type):
        self.writes.append(("set_margin_type", margin_type))
        self.margin_type = margin_type.lower()

    def place_market_order(self, *a, **kw):  # pragma: no cover
        raise AssertionError("a test attempted to place a real order")


class FakeCredentials:
    def __init__(self, user_id):
        self.user_id = user_id

    def fetch(self):
        return CredentialsResult(
            credentials=UserCredentials(f"key-{self.user_id}", "secret", "abcd")
        )


def host_limits():
    return HostLimits(
        env_mode="LIVE_TRADE",
        base_url="https://fapi.binance.com",
        trade_capable=True,
        ack_present=True,
        app_api_base="https://app.example.test",
        engine_service_token="service-token",
        is_live=True,
    )


def make_session(monkeypatch, user_id, client, config=None):
    """A real UserSession wired to one fake Binance account."""
    session = UserSession(
        user_id=user_id,
        limits=host_limits(),
        credentials_client=FakeCredentials(user_id),
        env_credentials=None,
        reporter=None,
        start_after=None,
        symbol=SYMBOL,
        smoke_side=None,
    )
    cfg = dict(BASE_CONFIG if config is None else config)
    monkeypatch.setattr(session._consumer, "_get", lambda path, params: {"config": cfg})
    # The credential fetch would otherwise build a real client.
    monkeypatch.setattr(session, "_build_client", lambda key, secret: client)
    # poll_once fetches pending signals over HTTP; the cycle under test ends
    # before any order is built, so stub it out rather than serve fake signals.
    monkeypatch.setattr(session._consumer, "poll_once", lambda **kw: None)
    monkeypatch.setattr(session._consumer, "reconcile", lambda amt: (False, None))
    return session


# --- the capital base is this user's own totalWalletBalance --------------- #

def test_cycle_feeds_total_wallet_balance_into_the_formula(monkeypatch):
    client = FakeClient("https://fapi.binance.com", "k", "s", wallet="300")
    session = make_session(monkeypatch, ALICE, client)
    session.run_cycle()

    consumer = session._consumer
    assert consumer.wallet_balance == Decimal("300")
    # 300 x 20% x 30 = 1800, from the exchange reading alone.
    assert consumer._target_notional() == Decimal("1800")


def test_available_balance_is_carried_but_is_not_the_base(monkeypatch):
    """A wallet with margin posted elsewhere still allocates off the full
    wallet; availableBalance only decides whether the margin can be posted."""
    client = FakeClient(
        "https://fapi.binance.com", "k", "s", wallet="300", available="120"
    )
    session = make_session(monkeypatch, ALICE, client)
    session.run_cycle()

    consumer = session._consumer
    assert consumer.wallet_balance == Decimal("300")
    assert consumer._available_balance == Decimal("120")
    assert consumer._target_notional() == Decimal("1800")


def test_a_missing_wallet_field_blocks_opens_rather_than_crashing(monkeypatch):
    client = FakeClient("https://fapi.binance.com", "k", "s")
    client.get_account = lambda: {"availableBalance": "300"}
    session = make_session(monkeypatch, ALICE, client)
    session.run_cycle()

    assert session._consumer.wallet_balance is None
    order = session._consumer._build_intent(
        {"rule_side": 1, "bar_time": "2026-08-24T00:00:00Z"}, Decimal("2500")
    )
    assert order["status"] == "SKIPPED"
    assert order["error"] == "wallet balance unknown"


# --- multi-tenant isolation ---------------------------------------------- #

def test_two_users_size_against_their_own_wallets(monkeypatch):
    alice_client = FakeClient("https://fapi.binance.com", "ak", "as", wallet="300")
    bob_client = FakeClient("https://fapi.binance.com", "bk", "bs", wallet="10000")

    alice = make_session(monkeypatch, ALICE, alice_client)
    bob = make_session(
        monkeypatch, BOB, bob_client, config=dict(BASE_CONFIG, capital_allocation_pct=50)
    )

    alice.run_cycle()
    bob.run_cycle()

    assert alice._consumer.wallet_balance == Decimal("300")
    assert bob._consumer.wallet_balance == Decimal("10000")
    assert alice._consumer._target_notional() == Decimal("1800")
    assert bob._consumer._target_notional() == Decimal("150000")

    # Bob cycling again does not disturb Alice's figures.
    bob_client.wallet = "50000"
    bob.run_cycle()
    assert alice._consumer.wallet_balance == Decimal("300")
    assert alice._consumer._target_notional() == Decimal("1800")


def test_each_session_signs_with_its_own_users_key(monkeypatch):
    """The wallet read is a signed call, so the key that made it is what decides
    whose wallet was read."""
    seen = {}

    def build(user_id, client):
        session = make_session(monkeypatch, user_id, client)
        monkeypatch.setattr(
            session,
            "_build_client",
            lambda key, secret, _c=client, _u=user_id: (
                seen.__setitem__(_u, key) or _c
            ),
        )
        return session

    alice = build(ALICE, FakeClient("https://fapi.binance.com", "ak", "as", wallet="300"))
    bob = build(BOB, FakeClient("https://fapi.binance.com", "bk", "bs", wallet="10000"))
    alice.run_cycle()
    bob.run_cycle()

    assert seen[ALICE] == f"key-{ALICE}"
    assert seen[BOB] == f"key-{BOB}"
    assert seen[ALICE] != seen[BOB]


# --- an open position defers the leverage change, CLOSE stays allowed ----- #

def test_open_position_defers_a_leverage_change_and_blocks_opens(monkeypatch, caplog):
    """Requirement: changing configuration must never resize, flip or close a
    position that is already open."""
    client = FakeClient(
        "https://fapi.binance.com",
        "k",
        "s",
        wallet="300",
        position_amt="0.5",   # a live LONG
        leverage="10",        # ... opened at a DIFFERENT leverage
    )
    session = make_session(monkeypatch, ALICE, client)

    captured = {}
    monkeypatch.setattr(
        session._consumer,
        "poll_once",
        lambda **kw: captured.update(kw),
    )
    session.run_cycle()

    # No write of any kind touched the open position.
    assert client.writes == []
    assert client.leverage == "10"
    assert client.position_amt == "0.5"
    # OPENs are blocked and the reason says why.
    assert captured["opens_blocked"] is True
    assert captured["block_reason"] == "leverage_config_mismatch"
    # ... and the position amount is still passed through, which is what a
    # CLOSE sizes from.
    assert captured["position_amt"] == 0.5


def test_close_is_still_permitted_while_opens_are_blocked(monkeypatch):
    """opens_blocked gates OPENs only. The CLOSE path never consults it, and a
    CLOSE sizes from the real Binance position amount."""
    client = FakeClient(
        "https://fapi.binance.com", "k", "s", position_amt="0.5", leverage="10"
    )
    session = make_session(monkeypatch, ALICE, client)
    session.run_cycle()

    close = session._consumer._build_close_intent(
        {"bar_time": "2026-08-24T00:00:00Z", "position_before": "LONG"},
        Decimal("2500"),
        0.5,
    )
    assert close["intent"] == "CLOSE"
    assert close["qty"] == 0.5
    assert close["status"] == "INTENT_LOGGED"


def test_leverage_is_written_when_the_account_is_flat(monkeypatch):
    client = FakeClient(
        "https://fapi.binance.com", "k", "s", position_amt="0", leverage="10"
    )
    session = make_session(monkeypatch, ALICE, client)
    session.run_cycle()

    assert ("set_leverage", 30) in client.writes
    assert client.leverage == "30"


def test_leverage_is_clamped_to_the_exchange_maximum(monkeypatch, caplog):
    """The bracket probe reports 90x as the ceiling; a higher configured value
    is clamped to it rather than sent to Binance as-is."""
    client = FakeClient("https://fapi.binance.com", "k", "s", position_amt="0")
    session = make_session(
        monkeypatch, ALICE, client, config=dict(BASE_CONFIG, leverage=95)
    )
    session.run_cycle()

    written = [lev for name, lev in client.writes if name == "set_leverage"]
    assert written == [90]
    assert "CLAMPED" in caplog.text


def test_exchange_rejection_of_leverage_blocks_opens_without_raising(monkeypatch):
    """Binance -4028 means the leverage is not permitted for this account. The
    executor must block OPENs, not crash and not trade at a leverage it never
    successfully set."""
    from binance_client import BinanceAPIError

    client = FakeClient(
        "https://fapi.binance.com",
        "k",
        "s",
        position_amt="0",
        leverage="10",
        set_leverage_error=BinanceAPIError(400, -4028, "leverage not valid"),
    )
    session = make_session(monkeypatch, ALICE, client)

    captured = {}
    monkeypatch.setattr(session._consumer, "poll_once", lambda **kw: captured.update(kw))
    session.run_cycle()

    assert captured["opens_blocked"] is True
    assert client.leverage == "10"  # never moved


def test_a_non_isolated_margin_type_while_flat_is_fixed_not_ignored(monkeypatch):
    client = FakeClient(
        "https://fapi.binance.com", "k", "s", position_amt="0", margin_type="cross"
    )
    session = make_session(monkeypatch, ALICE, client)
    session.run_cycle()
    assert ("set_margin_type", "ISOLATED") in client.writes
