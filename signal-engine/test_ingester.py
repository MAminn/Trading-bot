"""Unit tests for the engine -> signal-API bridge.

Pure and offline. The engine is replaced by a fake module object, the outbox is
a tracing subclass, and every poster is a stub: no engine import, no model load,
no HTTP, no Binance, no database, no container.

Run from signal-engine/:  python -m pytest test_ingester.py -q
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import ingester
from ingester import (
    FORBIDDEN_SIGNAL_FIELDS,
    Bridge,
    IngesterRecoveryError,
    build_signal_payload,
    build_trade_payload,
    _epoch_ms,
    _to_iso_z,
)
from outbox import HELD, PREPARED, READY, Outbox, canonical_json

BAR = "2026-08-30T10:00:00"
BAR_KEY = "2026-08-30T10:00:00Z"
NEXT_BAR = "2026-08-30T10:15:00"

# The exact set the app's Zod schema accepts, for this event.
EXPECTED_SIGNAL_KEYS = {
    "bar_time",
    "bar_closed_now",
    "valid_next_entry",
    "rule_side",
    "rule_reason",
    "ml_prob",
    "ml_threshold",
    "ml_accept",
    "opened",
    "closed_reason",
    "position_before",
    "position_after",
    "trade_id",
}


def make_event(t=BAR, **over):
    """A realistic close+open event, including everything that must NOT leak."""
    ev = {
        "run_id": "20260830T100000Z_abc123def4",
        "t": t,
        "bar_closed_now": True,
        "valid_next_entry": True,
        "rule_side": -1,
        "rule_reason": "fam__VOLATILITY|momentum_break|ML_ACCEPT",
        "ml_prob": 0.5123456789,
        "ml_threshold": 0.44,
        "ml_accept": True,
        "opened": "SHORT",
        "closed_reason": "V22_NORMAL_SL",
        "exit_px": 2500.5,
        "position_before": "LONG",
        "position_after": "SHORT",
        "position_before_state": {"trade_id": "OLD_LONG", "side": 1},
        "position_after_state": {"trade_id": "NEW_SHORT", "side": -1},
        "closed_position_state": {"trade_id": "OLD_LONG", "side": 1},
        "candidate_evaluation_blocked_by_position": False,
        "leverage_scenarios_json": '[{"scenario":"middle","leverage":30.0}]',
        "sample_features": {"rv_20": 1.0, "atrp_14": 2.0},
        "candidate_evaluations": [{"side": -1, "prob": 0.51}],
        "pre_ml_selector": {"long_selected": False, "short_selected": True},
        "rule_funnel": {"long": {}, "short": {}},
        # Injected into the SAME dict by add_open/close_leverage_columns.
        "conservative_leverage": 1.0,
        "conservative_capital_usd": 80.0,
        "middle_leverage": 30.0,
        "middle_capital_usd": 80.0,
        "aggressive_leverage": 70.0,
        "aggressive_capital_usd": 80.0,
    }
    ev.update(over)
    return ev


def make_trade_row(**over):
    row = {
        "logged_at_utc": "2026-08-30T10:15:00.123456+00:00",
        "trade_id": "20240403T140000_LONG_V22_LONG",
        "status": "CLOSED",
        "side": "LONG",
        "setup_name": "V22_LONG",
        "signal_t": "2026-08-30T09:45:00+00:00",
        "entry_t": "2026-08-30T10:00:00+00:00",
        "exit_t": "2026-08-30T10:15:00+00:00",
        "entry": 2500.0,
        "exit": 2510.0,
        "tp": 2530.0,
        "initial_sl": 2480.0,
        "final_stop": 2495.0,
        "atr": 19.19,
        "bars_held": 4,
        "prob": 0.61,
        "threshold": 0.49,
        "exit_reason": "V22_TRAIL",
        "gross_pnl_rate": 0.004,
        "net_pnl_rate_after_round_trip_cost": 0.0028,
        "round_trip_cost": 0.0012,
        "best_high": 2515.0,
        "best_low": 2498.0,
        "mfe_atr": 0.78,
        "mae_atr": 0.10,
        "trail_active_at_exit": True,
        "path_bar_count": 4,
        "trade_path_json": "[]",
        "leverage_scenarios_json": '[{"scenario":"middle","leverage":30.0}]',
    }
    row.update(over)
    return row


class FakeEngine:
    """Stands in for engine/live_code.py. No engine code is imported."""

    def __init__(self, state_file, trace):
        self.RUNTIME_STATE_FILE = str(state_file)
        self.trace = trace
        self.persist_should_fail = False
        self.emit_trade = None
        self.persist_modes = []
        self.trade_rows = []

    def persist_bar_audits_and_state(self, row, event, state, live_price, diag, mode):
        self.trace.append("original")
        self.persist_modes.append(mode)
        if self.emit_trade is not None:
            # The engine calls its own (patched) trade writer from inside here.
            self._append_shadow_trade_row(self.emit_trade)
        if self.persist_should_fail:
            raise RuntimeError("persist failed")
        return "persisted"

    def _append_shadow_trade_row(self, row):
        self.trade_rows.append(row)
        return True


class TracingOutbox(Outbox):
    """Real outbox, with call tracing and fault injection. outbox.py untouched."""

    def __init__(self, *a, trace=None, **kw):
        super().__init__(*a, **kw)
        self.trace = trace if trace is not None else []
        self.prepare_should_fail = False
        self.promote_should_fail = False

    def prepare(self, *a, **kw):
        self.trace.append("prepare")
        if self.prepare_should_fail:
            raise sqlite3.OperationalError("disk I/O error")
        return super().prepare(*a, **kw)

    def promote(self, *a, **kw):
        self.trace.append("promote")
        if self.promote_should_fail:
            raise sqlite3.OperationalError("database or disk is full")
        return super().promote(*a, **kw)

    def detect_skipped_orphans(self, *a, **kw):
        self.trace.append("sweep")
        return super().detect_skipped_orphans(*a, **kw)


class TradePoster:
    def __init__(self, raises=False, ok=True):
        self.calls = []
        self.raises = raises
        self.ok = ok

    def __call__(self, path, payload):
        self.calls.append((path, payload))
        if self.raises:
            raise ConnectionError("network down")
        return self.ok


@pytest.fixture
def trace():
    return []


@pytest.fixture
def rig(tmp_path, trace):
    """(bridge, engine, outbox, trade_poster) with everything stubbed."""
    state_file = tmp_path / "shadow_live_state.json"
    outbox = TracingOutbox(str(tmp_path / "outbox.db"), poster=lambda p, b: (True, 200, None), trace=trace)
    trade_poster = TradePoster()
    bridge = Bridge(outbox=outbox, trade_poster=trade_poster)
    engine = FakeEngine(state_file, trace)
    bridge.attach(engine, start_threads=False)
    yield bridge, engine, outbox, trade_poster
    outbox.close()


def call(engine, event, mode="LIVE_SCHEDULED"):
    return engine.persist_bar_audits_and_state({}, event, {}, {}, {}, mode)


# ------------------------------------------------------------------ #
# payload allowlist
# ------------------------------------------------------------------ #

def test_signal_payload_is_exactly_the_allowlist():
    assert set(build_signal_payload(make_event())) == EXPECTED_SIGNAL_KEYS


def test_forbidden_fields_never_leak():
    p = build_signal_payload(make_event())
    for field in FORBIDDEN_SIGNAL_FIELDS:
        assert field not in p


def test_leverage_and_capital_never_leak():
    """A denylist would miss a scenario added later; the allowlist cannot."""
    p = build_signal_payload(make_event())
    for key in p:
        assert "leverage" not in key
        assert "capital" not in key


def test_t_is_renamed_to_bar_time():
    p = build_signal_payload(make_event())
    assert "t" not in p
    assert p["bar_time"] == BAR_KEY


def test_trade_id_prefers_the_open_position():
    assert build_signal_payload(make_event())["trade_id"] == "NEW_SHORT"


def test_trade_id_falls_back_to_the_closed_position():
    ev = make_event(position_after_state=None)
    assert build_signal_payload(ev)["trade_id"] == "OLD_LONG"


def test_trade_id_absent_when_no_position_state():
    ev = make_event(position_after_state=None, closed_position_state=None)
    assert "trade_id" not in build_signal_payload(ev)


def test_none_fields_are_dropped_not_nulled():
    """Zod .optional() rejects null but accepts a missing key."""
    ev = make_event(ml_prob=None, ml_accept=None, opened=None, closed_reason=None)
    p = build_signal_payload(ev)
    for key in ("ml_prob", "ml_accept", "opened", "closed_reason"):
        assert key not in p


# ------------------------------------------------------------------ #
# normalisation
# ------------------------------------------------------------------ #

def test_nan_and_inf_are_dropped():
    ev = make_event(ml_prob=float("nan"), ml_threshold=float("inf"))
    p = build_signal_payload(ev)
    assert "ml_prob" not in p
    assert "ml_threshold" not in p


def test_offset_datetime_is_normalised_to_z():
    assert _to_iso_z("2026-08-30T10:00:00+00:00") == "2026-08-30T10:00:00Z"


def test_naive_datetime_is_treated_as_utc():
    assert _to_iso_z("2026-08-30T10:00:00") == "2026-08-30T10:00:00Z"


def test_non_utc_offset_is_converted():
    assert _to_iso_z("2026-08-30T12:00:00+02:00") == "2026-08-30T10:00:00Z"


def test_epoch_ms_matches_across_formats():
    assert _epoch_ms("2026-08-30T10:00:00") == _epoch_ms("2026-08-30T10:00:00Z")


# ------------------------------------------------------------------ #
# trade payload
# ------------------------------------------------------------------ #

def test_trade_renames_initial_sl_and_net_pnl():
    p = build_trade_payload(make_trade_row())
    assert p["sl"] == 2480.0 and "initial_sl" not in p
    assert p["net_pnl_rate"] == 0.0028 and "net_pnl_rate_after_round_trip_cost" not in p


def test_trade_drops_fields_outside_the_schema():
    p = build_trade_payload(make_trade_row())
    for key in (
        "logged_at_utc", "status", "gross_pnl_rate", "best_high", "best_low",
        "mfe_atr", "mae_atr", "trail_active_at_exit", "path_bar_count",
        "trade_path_json", "leverage_scenarios_json",
    ):
        assert key not in p


def test_trade_datetimes_normalised_and_nan_dropped():
    p = build_trade_payload(make_trade_row(atr=float("nan")))
    assert p["exit_t"] == "2026-08-30T10:15:00Z"
    assert p["signal_t"].endswith("Z")
    assert "atr" not in p


# ------------------------------------------------------------------ #
# LIVE ordering
# ------------------------------------------------------------------ #

def test_live_orders_prepare_then_original_then_promote(rig, trace):
    bridge, engine, outbox, _ = rig
    call(engine, make_event())
    assert trace == ["prepare", "original", "promote", "sweep"]
    assert outbox.get(BAR_KEY).status == READY


def test_live_stores_the_canonical_payload(rig):
    bridge, engine, outbox, _ = rig
    ev = make_event()
    call(engine, ev)
    assert outbox.get(BAR_KEY).payload == canonical_json(build_signal_payload(ev))


def test_sender_cannot_see_prepared_before_promote(tmp_path, trace):
    """The row must not be deliverable until the engine's state is durable."""
    seen = []
    outbox = TracingOutbox(
        str(tmp_path / "o.db"),
        poster=lambda p, b: (seen.append(b), (True, 200, None))[1],
        trace=trace,
    )
    bridge = Bridge(outbox=outbox, trade_poster=TradePoster())
    engine = FakeEngine(tmp_path / "s.json", trace)

    def persist(row, event, state, live_price, diag, mode):
        trace.append("original")
        # Mid-persist: engine state is NOT yet durable.
        assert outbox.get(BAR_KEY).status == PREPARED
        assert outbox.deliver_once() == "BLOCKED"
        assert seen == []
        return None

    engine.persist_bar_audits_and_state = persist
    bridge.attach(engine, start_threads=False)
    engine.persist_bar_audits_and_state({}, make_event(), {}, {}, {}, "LIVE_SCHEDULED")
    assert outbox.deliver_once() == "DELIVERED"
    assert seen and seen[0]["bar_time"] == BAR_KEY
    outbox.close()


# ------------------------------------------------------------------ #
# CATCHUP
# ------------------------------------------------------------------ #

def test_catchup_creates_no_signal_row(rig, trace):
    bridge, engine, outbox, _ = rig
    call(engine, make_event(), mode="CATCHUP")
    assert outbox.get(BAR_KEY) is None
    assert "prepare" not in trace
    assert "original" in trace


def test_catchup_still_runs_orphan_detection(rig, trace):
    """Must not sit behind an early return, or an old orphan is never detected."""
    bridge, engine, outbox, _ = rig
    call(engine, make_event(), mode="CATCHUP")
    assert trace == ["original", "sweep"]


def test_catchup_holds_a_skipped_orphan(rig):
    bridge, engine, outbox, _ = rig
    outbox.prepare("2026-08-30T09:00:00Z", _epoch_ms("2026-08-30T09:00:00"), "/p", {"x": 1})
    call(engine, make_event(), mode="CATCHUP")
    assert outbox.get("2026-08-30T09:00:00Z").status == HELD


def test_catchup_emits_no_trade(rig):
    bridge, engine, outbox, trade_poster = rig
    engine.emit_trade = make_trade_row()
    call(engine, make_event(), mode="CATCHUP")
    assert trade_poster.calls == []


# ------------------------------------------------------------------ #
# interrupted-LIVE recovery
# ------------------------------------------------------------------ #

def test_interrupted_live_recovered_even_when_mode_is_catchup(rig, trace):
    """The regression the two-phase design exists for.

    A surviving PREPARED row proves the bar was LIVE, so ordinary CATCHUP
    suppression must not apply to it.
    """
    bridge, engine, outbox, _ = rig
    ev = make_event()
    outbox.prepare(BAR_KEY, _epoch_ms(BAR), "/api/public/engine/ingest/signal",
                   build_signal_payload(ev))
    trace.clear()
    call(engine, ev, mode="CATCHUP")
    assert trace == ["original", "promote", "sweep"]
    assert outbox.get(BAR_KEY).status == READY


def test_recovery_with_matching_payload_promotes(rig):
    bridge, engine, outbox, _ = rig
    ev = make_event()
    outbox.prepare(BAR_KEY, _epoch_ms(BAR), "/p", build_signal_payload(ev))
    call(engine, ev, mode="LIVE_SCHEDULED")
    assert outbox.get(BAR_KEY).status == READY


def test_recovery_mismatch_holds_raises_and_blocks_advancement(rig, trace):
    bridge, engine, outbox, _ = rig
    stored = make_event(ml_accept=True, rule_side=-1)
    outbox.prepare(BAR_KEY, _epoch_ms(BAR), "/p", build_signal_payload(stored))
    trace.clear()

    diverged = make_event(ml_accept=False, rule_side=1, opened=None)
    with pytest.raises(IngesterRecoveryError):
        call(engine, diverged, mode="LIVE_SCHEDULED")

    assert outbox.get(BAR_KEY).status == HELD
    assert "original" not in trace, "engine must not advance past a diverged bar"
    assert engine.persist_modes == []


def test_held_row_is_never_delivered(rig):
    bridge, engine, outbox, _ = rig
    outbox.prepare(BAR_KEY, _epoch_ms(BAR), "/p", build_signal_payload(make_event()))
    with pytest.raises(IngesterRecoveryError):
        call(engine, make_event(rule_side=1), mode="LIVE_SCHEDULED")
    assert outbox.deliver_once() == "BLOCKED"


# ------------------------------------------------------------------ #
# failure isolation
# ------------------------------------------------------------------ #

def test_sqlite_prepare_failure_prevents_original_persist(rig, trace):
    """Local durability failure must fail the bar, not silently continue."""
    bridge, engine, outbox, _ = rig
    outbox.prepare_should_fail = True
    with pytest.raises(sqlite3.OperationalError):
        call(engine, make_event())
    assert engine.persist_modes == []
    assert "original" not in trace


def test_original_persist_failure_leaves_row_prepared(rig):
    """The row survives so the interrupted transaction can be completed."""
    bridge, engine, outbox, _ = rig
    engine.persist_should_fail = True
    with pytest.raises(RuntimeError):
        call(engine, make_event())
    row = outbox.get(BAR_KEY)
    assert row is not None and row.status == PREPARED


def test_promote_failure_does_not_roll_engine_state_backward(rig):
    """Engine state is already durable; raising here would undo a committed bar."""
    bridge, engine, outbox, _ = rig
    outbox.promote_should_fail = True
    call(engine, make_event())              # must NOT raise
    assert engine.persist_modes == ["LIVE_SCHEDULED"]
    assert outbox.get(BAR_KEY).status == PREPARED   # startup recovery promotes it


def test_trade_post_failure_is_swallowed(rig):
    bridge, engine, outbox, _ = rig
    bridge.trade_poster = TradePoster(raises=True)
    engine.emit_trade = make_trade_row()
    call(engine, make_event())              # must NOT raise
    assert outbox.get(BAR_KEY).status == READY


def test_live_emits_trade_best_effort(rig):
    bridge, engine, outbox, trade_poster = rig
    engine.emit_trade = make_trade_row()
    call(engine, make_event())
    assert len(trade_poster.calls) == 1
    path, payload = trade_poster.calls[0]
    assert path == "/api/public/engine/ingest/trade"
    assert payload["sl"] == 2480.0


def test_duplicate_trade_row_is_not_reposted(rig):
    """The engine's own dedupe returns False; nothing should be emitted."""
    bridge, engine, outbox, trade_poster = rig
    engine._append_shadow_trade_row = bridge.trade_hook(lambda row: False)
    engine.emit_trade = make_trade_row()
    call(engine, make_event())
    assert trade_poster.calls == []


# ------------------------------------------------------------------ #
# recovery from the engine state file
# ------------------------------------------------------------------ #

def test_recover_promotes_when_bar_already_durable(tmp_path, trace):
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"last_processed_bar": BAR}), encoding="utf-8")
    outbox = TracingOutbox(str(tmp_path / "o.db"), poster=lambda p, b: (True, 200, None), trace=trace)
    outbox.prepare(BAR_KEY, _epoch_ms(BAR), "/p", {"bar_time": BAR_KEY})
    Bridge(outbox=outbox, trade_poster=TradePoster()).recover_from_state_file(state)
    assert outbox.get(BAR_KEY).status == READY
    outbox.close()


def test_recover_keeps_interrupted_bar_prepared(tmp_path, trace):
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"last_processed_bar": BAR}), encoding="utf-8")
    outbox = TracingOutbox(str(tmp_path / "o.db"), poster=lambda p, b: (True, 200, None), trace=trace)
    key = _to_iso_z(NEXT_BAR)
    outbox.prepare(key, _epoch_ms(NEXT_BAR), "/p", {"bar_time": key})
    Bridge(outbox=outbox, trade_poster=TradePoster()).recover_from_state_file(state)
    row = outbox.get(key)
    assert row is not None and row.status == PREPARED
    outbox.close()


def test_recover_tolerates_a_missing_state_file(tmp_path, trace):
    outbox = TracingOutbox(str(tmp_path / "o.db"), poster=lambda p, b: (True, 200, None), trace=trace)
    outbox.prepare(BAR_KEY, _epoch_ms(BAR), "/p", {"bar_time": BAR_KEY})
    Bridge(outbox=outbox, trade_poster=TradePoster()).recover_from_state_file(tmp_path / "nope.json")
    assert outbox.get(BAR_KEY).status == PREPARED
    outbox.close()


# ------------------------------------------------------------------ #
# attachment and heartbeat
# ------------------------------------------------------------------ #

def test_attach_patches_both_entry_points_without_running_the_engine(tmp_path, trace):
    outbox = TracingOutbox(str(tmp_path / "o.db"), poster=lambda p, b: (True, 200, None), trace=trace)
    engine = FakeEngine(tmp_path / "s.json", trace)
    before = engine.persist_bar_audits_and_state
    Bridge(outbox=outbox, trade_poster=TradePoster()).attach(engine, start_threads=False)
    assert engine.persist_bar_audits_and_state is not before
    assert trace == []          # attaching runs no engine code
    outbox.close()


def test_heartbeat_is_running_when_lane_is_healthy(rig):
    bridge, engine, outbox, _ = rig
    hb = bridge.heartbeat_payload()
    assert hb["status"] == "running"
    assert hb["current_position"] == "FLAT"


def test_heartbeat_tracks_position_from_events(rig):
    bridge, engine, outbox, _ = rig
    call(engine, make_event())
    assert bridge.heartbeat_payload()["current_position"] == "SHORT"


def test_heartbeat_reports_error_when_lane_is_held(rig):
    bridge, engine, outbox, _ = rig
    outbox.prepare(BAR_KEY, _epoch_ms(BAR), "/p", {"bar_time": BAR_KEY})
    outbox.hold(BAR_KEY, "payload divergence on recovery")
    hb = bridge.heartbeat_payload()
    assert hb["status"] == "error"
    assert "held" in hb["message"]


def test_post_once_without_credentials_reports_failure_and_never_raises():
    ok, status, error = ingester.post_once("/x", {"a": 1})
    assert ok is False
    assert status is None
    assert "APP_API_BASE" in (error or "")
