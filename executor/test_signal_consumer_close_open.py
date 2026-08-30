"""Same-bar CLOSE+OPEN, replay idempotency, and stale-OPEN safety.

The engine can close a position and open the next one from a single decision on
the same signal bar — 154 of the 2922 historical trades, 85 of them side
reversals. The executor previously identified a close by
`position_after == "FLAT"`, which is false on exactly those bars, so the close
half was silently skipped and a real position was left open with no exit (the
executor places only MARKET orders, so the engine's close signal is a position's
only exit).

These tests hold down the corrected dispatch and everything it must not break.

No network, no exchange, no database: the consumer is constructed and its
collaborators are monkeypatched. `_post_order` and `_place_order` are recorded
rather than performed, so nothing is ever sent anywhere.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from risk_guard import RiskGuard
from signal_consumer import (
    MAX_OPEN_AGE_SECONDS,
    SignalConsumer,
    open_is_fresh,
)

USER = "11111111-1111-1111-1111-111111111111"
SYMBOL = "ETHUSDT"
MARK = 2500.0
LONG_AMT = 0.5
SHORT_AMT = -0.5


# ---------------------------------------------------------------------- #
# doubles
# ---------------------------------------------------------------------- #

class FakeMark:
    """The one unsigned read _poll_signals makes before sizing anything."""

    def get_mark_price(self, symbol):
        return MARK


class Recorder:
    """Captures intents and placements instead of performing them."""

    def __init__(self, settle=None, duplicates=()):
        self.intents: list[dict] = []
        self.placed: list[dict] = []
        self.duplicates = set(duplicates)
        self._settle = settle

    # stands in for _post_order: False == the app reported HTTP 409
    def post_order(self, order):
        self.intents.append(dict(order))
        return order["idempotency_key"] not in self.duplicates

    # stands in for _place_order: returns the settled position amount
    def place_order(self, order):
        self.placed.append(dict(order))
        if self._settle is not None:
            return self._settle(order)
        if order["intent"] == "CLOSE":
            return 0.0
        return order["qty"] if order["side"] == "LONG" else -order["qty"]

    def of(self, intent, collection=None):
        rows = self.intents if collection is None else collection
        return [o for o in rows if o["intent"] == intent]


def make_consumer(monkeypatch, signals, *, mode="LIVE_TRADE", recorder=None,
                  trade_capable=True):
    c = SignalConsumer("http://app.invalid", "tok", USER, mode, SYMBOL)
    c._binance = FakeMark()
    c.set_account_balances(wallet_balance="300", available_balance="300")
    c._alloc_pct = Decimal("20")
    c._leverage = Decimal("30")
    c._bracket_notional_cap = None
    recorder = recorder or Recorder()

    if trade_capable:
        c._risk_guard = RiskGuard(max_leverage=90)
        c._binance_trader = object()      # a placement path exists
    else:
        c._risk_guard = None
        c._binance_trader = None          # read-only: nothing may be placed

    monkeypatch.setattr(c, "_get", lambda path, params: list(signals))
    monkeypatch.setattr(c, "_post_order", recorder.post_order)
    monkeypatch.setattr(c, "_post_order_update", lambda update: None)
    monkeypatch.setattr(c, "_place_order", recorder.place_order)
    return c, recorder


# ---------------------------------------------------------------------- #
# signal builders
# ---------------------------------------------------------------------- #

def _iso(dt):
    return dt.replace(tzinfo=None).isoformat() + "Z"


def fresh_bar(seconds_old=60):
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds_old))


def stale_bar(seconds_old=MAX_OPEN_AGE_SECONDS + 60):
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds_old))


def signal(**over):
    s = {
        "id": "sig-1",
        "bar_time": fresh_bar(),
        "created_at": "2026-08-30T10:16:00Z",
        "rule_side": 0,
        "ml_accept": None,
        "opened": None,
        "closed_reason": None,
        "position_before": "FLAT",
        "position_after": "FLAT",
    }
    s.update(over)
    return s


# Each builder collects its defaults into a dict and lets **over update them, so
# a caller may override any field — including one the builder sets itself —
# without colliding on a duplicate keyword argument.

def close_only(side="LONG", **over):
    fields = {
        "closed_reason": "V22_NORMAL_SL",
        "position_before": side,
        "position_after": "FLAT",
    }
    fields.update(over)
    return signal(**fields)


def open_only(side="LONG", **over):
    fields = {
        "rule_side": 1 if side == "LONG" else -1,
        "ml_accept": True,
        "opened": side,
        "position_before": "FLAT",
        "position_after": side,
    }
    fields.update(over)
    return signal(**fields)


def close_open(before="LONG", after="SHORT", **over):
    fields = {
        "closed_reason": "V22_NORMAL_SL",
        "position_before": before,
        "rule_side": 1 if after == "LONG" else -1,
        "ml_accept": True,
        "opened": after,
        "position_after": after,
    }
    fields.update(over)
    return signal(**fields)


def key(user, bar_time, side, intent):
    return f"{user}:{bar_time}:{side}:{intent}"


# ---------------------------------------------------------------------- #
# regressions: the two shapes that already worked must be untouched
# ---------------------------------------------------------------------- #

def test_close_only_still_closes(monkeypatch):
    c, rec = make_consumer(monkeypatch, [close_only("LONG")])
    c._poll_signals(LONG_AMT, False, None)
    assert len(rec.of("CLOSE", rec.placed)) == 1
    assert rec.of("OPEN", rec.placed) == []
    assert rec.placed[0]["side"] == "LONG"


def test_open_only_still_opens(monkeypatch):
    c, rec = make_consumer(monkeypatch, [open_only("LONG")])
    c._poll_signals(0.0, False, None)
    assert len(rec.of("OPEN", rec.placed)) == 1
    assert rec.of("CLOSE", rec.placed) == []


def test_open_only_from_flat_builds_no_close_intent(monkeypatch):
    c, rec = make_consumer(monkeypatch, [open_only("SHORT")])
    c._poll_signals(0.0, False, None)
    assert rec.of("CLOSE") == []


def test_flat_bar_does_nothing(monkeypatch):
    c, rec = make_consumer(monkeypatch, [signal()])
    c._poll_signals(0.0, False, None)
    assert rec.intents == []
    assert rec.placed == []


# ---------------------------------------------------------------------- #
# same-bar close + open
# ---------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "before,after,start_amt",
    [
        ("LONG", "LONG", LONG_AMT),
        ("LONG", "SHORT", LONG_AMT),
        ("SHORT", "LONG", SHORT_AMT),
    ],
)
def test_close_open_executes_both_halves(monkeypatch, before, after, start_amt):
    c, rec = make_consumer(monkeypatch, [close_open(before, after)])
    c._poll_signals(start_amt, False, None)

    assert [o["intent"] for o in rec.placed] == ["CLOSE", "OPEN"]
    assert rec.placed[0]["side"] == before      # closes what was held
    assert rec.placed[1]["side"] == after       # opens the new side


def test_close_is_always_processed_before_open(monkeypatch):
    c, rec = make_consumer(monkeypatch, [close_open("LONG", "SHORT")])
    c._poll_signals(LONG_AMT, False, None)
    intents = [o["intent"] for o in rec.intents]
    assert intents.index("CLOSE") < intents.index("OPEN")


def test_close_and_open_use_distinct_idempotency_keys(monkeypatch):
    """Distinct even for LONG->LONG, via the intent suffix."""
    sig = close_open("LONG", "LONG")
    c, rec = make_consumer(monkeypatch, [sig])
    c._poll_signals(LONG_AMT, False, None)
    keys = [o["idempotency_key"] for o in rec.intents]
    assert len(set(keys)) == 2
    assert keys[0].endswith(":LONG:CLOSE")
    assert keys[1].endswith(":LONG:OPEN")


def test_open_is_sized_after_the_close_settles(monkeypatch):
    """The OPEN must be evaluated against the flattened position, or the risk
    guard's one-position rule would reject it."""
    c, rec = make_consumer(monkeypatch, [close_open("LONG", "SHORT")])
    c._poll_signals(LONG_AMT, False, None)
    open_intent = rec.of("OPEN")[0]
    assert open_intent["status"] == "INTENT_LOGGED"
    assert open_intent["qty"] > 0


def test_ml_accept_false_closes_but_does_not_open(monkeypatch):
    c, rec = make_consumer(monkeypatch, [close_open("LONG", "SHORT", ml_accept=False)])
    c._poll_signals(LONG_AMT, False, None)
    assert len(rec.of("CLOSE", rec.placed)) == 1
    assert rec.of("OPEN", rec.placed) == []
    assert rec.of("OPEN") == []


# ---------------------------------------------------------------------- #
# close failure suppresses the open
# ---------------------------------------------------------------------- #

def test_open_suppressed_when_close_does_not_flatten(monkeypatch):
    """Explicitly, not by relying on the guard's 'position already open'."""
    rec = Recorder(settle=lambda o: LONG_AMT if o["intent"] == "CLOSE" else 0.0)
    c, rec = make_consumer(monkeypatch, [close_open("LONG", "SHORT")], recorder=rec)
    c._poll_signals(LONG_AMT, False, None)
    assert len(rec.of("CLOSE", rec.placed)) == 1
    assert rec.of("OPEN", rec.placed) == []
    assert rec.of("OPEN") == [], "no OPEN intent may even be recorded"


def test_open_suppressed_when_close_placement_fails(monkeypatch):
    """_place_order returning None leaves the position untouched."""
    rec = Recorder(settle=lambda o: None)
    c, rec = make_consumer(monkeypatch, [close_open("LONG", "SHORT")], recorder=rec)
    c._poll_signals(LONG_AMT, False, None)
    assert rec.of("OPEN", rec.placed) == []


def test_suppression_is_logged_as_an_error(monkeypatch, caplog):
    rec = Recorder(settle=lambda o: LONG_AMT if o["intent"] == "CLOSE" else 0.0)
    c, rec = make_consumer(monkeypatch, [close_open("LONG", "SHORT")], recorder=rec)
    with caplog.at_level("ERROR"):
        c._poll_signals(LONG_AMT, False, None)
    assert "OPEN SUPPRESSED" in caplog.text


# ---------------------------------------------------------------------- #
# already-flat replay: no malformed qty=0 close
# ---------------------------------------------------------------------- #

def test_already_flat_builds_no_close_intent(monkeypatch):
    """CLOSE succeeded, crash before OPEN, signal seen again."""
    c, rec = make_consumer(monkeypatch, [close_open("LONG", "SHORT")])
    c._poll_signals(0.0, False, None)
    assert rec.of("CLOSE") == [], "a qty=0 CLOSE intent must never be built"
    assert len(rec.of("OPEN", rec.placed)) == 1


def test_already_flat_close_only_posts_nothing(monkeypatch):
    c, rec = make_consumer(monkeypatch, [close_only("LONG")])
    c._poll_signals(0.0, False, None)
    assert rec.intents == []
    assert rec.placed == []


def test_no_zero_quantity_close_intent_is_ever_posted(monkeypatch):
    for amt in (0.0, LONG_AMT, SHORT_AMT):
        c, rec = make_consumer(monkeypatch, [close_open("LONG", "SHORT")])
        c._poll_signals(amt, False, None)
        for order in rec.of("CLOSE"):
            assert order["qty"] > 0


# ---------------------------------------------------------------------- #
# replay after the whole transition already completed
# ---------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "before,after,replacement_amt",
    [("LONG", "SHORT", SHORT_AMT), ("LONG", "LONG", LONG_AMT)],
)
def test_replay_after_complete_transition(monkeypatch, before, after, replacement_amt):
    """Both halves already executed; the live position is the REPLACEMENT one.

    Neither half may act: the replacement must not be closed and must not be
    opened again. The app's 409 on both idempotency keys is what stops it.
    """
    sig = close_open(before, after)
    dup = {
        key(USER, sig["bar_time"], before, "CLOSE"),
        key(USER, sig["bar_time"], after, "OPEN"),
    }
    rec = Recorder(duplicates=dup)
    c, rec = make_consumer(monkeypatch, [sig], recorder=rec)
    c._poll_signals(replacement_amt, False, None)

    assert rec.placed == [], "a replay must place nothing"
    assert len(rec.of("CLOSE")) == 1     # posted, 409'd, never placed
    assert len(rec.of("OPEN")) == 1


def test_replay_does_not_log_a_false_suppression(monkeypatch, caplog):
    """The duplicate CLOSE marks the transition as already applied, so the OPEN
    branch must not report a failed close."""
    sig = close_open("LONG", "SHORT")
    dup = {
        key(USER, sig["bar_time"], "LONG", "CLOSE"),
        key(USER, sig["bar_time"], "SHORT", "OPEN"),
    }
    rec = Recorder(duplicates=dup)
    c, rec = make_consumer(monkeypatch, [sig], recorder=rec)
    with caplog.at_level("ERROR"):
        c._poll_signals(SHORT_AMT, False, None)
    assert "OPEN SUPPRESSED" not in caplog.text


def test_replay_reaches_the_open_branch_so_its_key_stops_the_double_open(monkeypatch):
    """Only the CLOSE key is duplicated: the OPEN must still be attempted, and
    is stopped by its own key rather than by the close-satisfied flag."""
    sig = close_open("LONG", "SHORT")
    rec = Recorder(duplicates={key(USER, sig["bar_time"], "LONG", "CLOSE")})
    c, rec = make_consumer(monkeypatch, [sig], recorder=rec)
    c._poll_signals(SHORT_AMT, False, None)
    assert len(rec.of("OPEN")) == 1
    assert rec.of("CLOSE", rec.placed) == []


# ---------------------------------------------------------------------- #
# stale-OPEN boundary
# ---------------------------------------------------------------------- #

def test_boundary_is_two_bars():
    assert MAX_OPEN_AGE_SECONDS == 1800


def test_open_is_fresh_just_before_the_boundary():
    bar = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
    now = bar + timedelta(seconds=MAX_OPEN_AGE_SECONDS - 1)
    assert open_is_fresh({"bar_time": _iso(bar)}, now_utc=now) is True


def test_open_is_stale_exactly_at_the_boundary():
    bar = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
    now = bar + timedelta(seconds=MAX_OPEN_AGE_SECONDS)
    assert open_is_fresh({"bar_time": _iso(bar)}, now_utc=now) is False


def test_open_freshness_accepts_offset_form():
    bar = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
    now = bar + timedelta(seconds=60)
    assert open_is_fresh({"bar_time": "2026-08-30T10:00:00+00:00"}, now_utc=now) is True


def test_open_freshness_fails_safe_on_unusable_bar_time():
    now = datetime.now(timezone.utc)
    assert open_is_fresh({}, now_utc=now) is False
    assert open_is_fresh({"bar_time": None}, now_utc=now) is False
    assert open_is_fresh({"bar_time": "not-a-date"}, now_utc=now) is False


def test_stale_open_is_recorded_skipped_and_not_placed(monkeypatch):
    c, rec = make_consumer(monkeypatch, [open_only("LONG", bar_time=stale_bar())])
    c._poll_signals(0.0, False, None)
    assert rec.placed == []
    assert len(rec.of("OPEN")) == 1
    assert rec.of("OPEN")[0]["status"] == "SKIPPED"
    assert "stale" in rec.of("OPEN")[0]["error"]


def test_fresh_open_is_placed(monkeypatch):
    c, rec = make_consumer(monkeypatch, [open_only("LONG", bar_time=fresh_bar())])
    c._poll_signals(0.0, False, None)
    assert len(rec.of("OPEN", rec.placed)) == 1


# ---------------------------------------------------------------------- #
# CLOSE is never age-gated
# ---------------------------------------------------------------------- #

def test_old_close_still_executes(monkeypatch):
    """Three hours late. The position's only exit is this signal."""
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=3))
    c, rec = make_consumer(monkeypatch, [close_only("LONG", bar_time=old)])
    c._poll_signals(LONG_AMT, False, None)
    assert len(rec.of("CLOSE", rec.placed)) == 1


def test_stale_close_open_closes_but_does_not_reopen(monkeypatch):
    """The delayed-delivery case: drain the risk, do not enter on a stale price."""
    c, rec = make_consumer(monkeypatch, [close_open("LONG", "SHORT", bar_time=stale_bar())])
    settled = c._poll_signals(LONG_AMT, False, None)  # noqa: F841

    assert len(rec.of("CLOSE", rec.placed)) == 1
    assert rec.of("OPEN", rec.placed) == []
    open_intent = rec.of("OPEN")[0]
    assert open_intent["status"] == "SKIPPED"
    assert "stale" in open_intent["error"]


def test_existing_block_reason_survives_a_stale_open(monkeypatch):
    """An operator block must keep its own reason, not be relabelled stale."""
    c, rec = make_consumer(monkeypatch, [open_only("LONG", bar_time=stale_bar())])
    c._poll_signals(0.0, True, "kill_switch_active")
    assert rec.of("OPEN")[0]["error"] == "kill_switch_active"


# ---------------------------------------------------------------------- #
# read-only modes
# ---------------------------------------------------------------------- #

@pytest.mark.parametrize("mode", ["TESTNET_READ", "LIVE_READ"])
def test_read_only_modes_place_nothing(monkeypatch, mode):
    c, rec = make_consumer(
        monkeypatch, [close_open("LONG", "SHORT")], mode=mode, trade_capable=False
    )
    c._poll_signals(LONG_AMT, False, None)
    assert rec.placed == [], "a read mode must never place an order"


@pytest.mark.parametrize("mode", ["TESTNET_READ", "LIVE_READ"])
def test_read_only_modes_still_record_both_intents(monkeypatch, mode):
    """A read mode cannot flatten, so the close must not be treated as failed —
    that would drop the OPEN intent and weaken the dry run."""
    c, rec = make_consumer(
        monkeypatch, [close_open("LONG", "SHORT")], mode=mode, trade_capable=False
    )
    c._poll_signals(LONG_AMT, False, None)
    assert len(rec.of("CLOSE")) == 1
    assert len(rec.of("OPEN")) == 1


def test_read_only_close_only_unchanged(monkeypatch):
    c, rec = make_consumer(
        monkeypatch, [close_only("LONG")], mode="LIVE_READ", trade_capable=False
    )
    c._poll_signals(LONG_AMT, False, None)
    assert len(rec.of("CLOSE")) == 1
    assert rec.placed == []


# ---------------------------------------------------------------------- #
# cursor
# ---------------------------------------------------------------------- #

def test_cursor_advances_after_both_halves(monkeypatch):
    sig = close_open("LONG", "SHORT", created_at="2026-08-30T10:16:00Z")
    c, rec = make_consumer(monkeypatch, [sig])
    c._poll_signals(LONG_AMT, False, None)
    assert c._cursor == "2026-08-30T10:16:00Z"
