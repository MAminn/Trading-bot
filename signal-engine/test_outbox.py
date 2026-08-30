"""Unit tests for the durable signal outbox.

Pure and offline: no engine import, no network, no container, no Binance. The
poster is a stub and the clock is injected, so backoff is asserted without
sleeping.

Run from signal-engine/:  python -m pytest test_outbox.py -q
"""
from __future__ import annotations

import pytest

from outbox import (
    ACKED,
    BACKOFF_CAP_SECONDS,
    BLOCKED,
    DELIVERED,
    ESCALATE_AFTER_ATTEMPTS,
    HELD,
    IDLE,
    PREPARED,
    READY,
    RETRY,
    WAITING,
    Outbox,
    canonical_json,
)

PATH = "/api/public/engine/ingest/signal"

# Three consecutive 15m bars as epoch ms.
BAR1 = 1_700_000_000_000
BAR2 = BAR1 + 900_000
BAR3 = BAR2 + 900_000


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakePoster:
    """Records calls and replays a scripted sequence of results."""

    def __init__(self, results=None) -> None:
        self.results = list(results or [])
        self.calls = []

    def __call__(self, path, payload):
        self.calls.append((path, payload))
        if self.results:
            return self.results.pop(0)
        return True, 200, None


def payload(bar_time: str, **over):
    p = {
        "bar_time": bar_time,
        "rule_side": -1,
        "ml_accept": True,
        "opened": "SHORT",
        "closed_reason": "V22_NORMAL_SL",
        "position_before": "LONG",
        "position_after": "SHORT",
    }
    p.update(over)
    return p


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def poster():
    return FakePoster()


@pytest.fixture
def box(tmp_path, clock, poster):
    ob = Outbox(str(tmp_path / "outbox.db"), poster=poster, clock=clock)
    yield ob
    ob.close()


# ------------------------------------------------------------------ #
# prepare / promote
# ------------------------------------------------------------------ #

def test_prepare_writes_prepared(box):
    box.prepare("t1", BAR1, PATH, payload("t1"))
    row = box.get("t1")
    assert row is not None
    assert row.status == PREPARED
    assert row.payload_dict()["bar_time"] == "t1"


def test_prepare_is_idempotent_and_refreshes_only_while_prepared(box):
    box.prepare("t1", BAR1, PATH, payload("t1", rule_side=-1))
    box.prepare("t1", BAR1, PATH, payload("t1", rule_side=1))
    assert box.stats() == {PREPARED: 1}
    assert box.get("t1").payload_dict()["rule_side"] == 1


def test_prepare_never_mutates_a_ready_row(box):
    """A redelivery may be in flight; its message must not change underneath."""
    box.prepare("t1", BAR1, PATH, payload("t1", rule_side=-1))
    box.promote("t1")
    box.prepare("t1", BAR1, PATH, payload("t1", rule_side=1))
    row = box.get("t1")
    assert row.status == READY
    assert row.payload_dict()["rule_side"] == -1


def test_prepare_never_mutates_a_held_row(box):
    box.prepare("t1", BAR1, PATH, payload("t1", rule_side=-1))
    box.hold("t1", "divergence")
    box.prepare("t1", BAR1, PATH, payload("t1", rule_side=1))
    row = box.get("t1")
    assert row.status == HELD
    assert row.payload_dict()["rule_side"] == -1


def test_promote_only_applies_to_prepared(box):
    box.prepare("t1", BAR1, PATH, payload("t1"))
    assert box.promote("t1") is True
    assert box.get("t1").status == READY
    assert box.promote("t1") is False


# ------------------------------------------------------------------ #
# sender: READY only, strict FIFO
# ------------------------------------------------------------------ #

def test_sender_never_delivers_prepared(box, poster):
    box.prepare("t1", BAR1, PATH, payload("t1"))
    assert box.deliver_once() == BLOCKED
    assert poster.calls == []


def test_sender_delivers_ready_and_acks(box, poster):
    box.prepare("t1", BAR1, PATH, payload("t1"))
    box.promote("t1")
    assert box.deliver_once() == DELIVERED
    assert len(poster.calls) == 1
    assert poster.calls[0][0] == PATH
    assert box.get("t1").status == ACKED


def test_empty_lane_is_idle(box):
    assert box.deliver_once() == IDLE


def test_prepared_head_blocks_a_later_ready_row(box, poster):
    """A CLOSE must never be overtaken by a later OPEN."""
    box.prepare("t1", BAR1, PATH, payload("t1"))          # stays PREPARED
    box.prepare("t2", BAR2, PATH, payload("t2"))
    box.promote("t2")                                      # newer row is READY
    assert box.deliver_once() == BLOCKED
    assert poster.calls == []

    box.promote("t1")
    assert box.deliver_once() == DELIVERED
    assert poster.calls[0][1]["bar_time"] == "t1"          # FIFO order
    assert box.deliver_once() == DELIVERED
    assert poster.calls[1][1]["bar_time"] == "t2"


def test_held_head_blocks_the_lane(box, poster):
    box.prepare("t1", BAR1, PATH, payload("t1"))
    box.hold("t1", "unrecoverable")
    box.prepare("t2", BAR2, PATH, payload("t2"))
    box.promote("t2")
    assert box.deliver_once() == BLOCKED
    assert poster.calls == []


# ------------------------------------------------------------------ #
# recovery: NEVER deletes
# ------------------------------------------------------------------ #

def test_recovery_window_b_promotes_when_bar_already_durable(box):
    box.prepare("t1", BAR1, PATH, payload("t1"))
    assert box.recover(last_processed_bar_ms=BAR1) == {"promoted": 1, "kept": 0}
    assert box.get("t1").status == READY


def test_recovery_window_b_promotes_when_engine_advanced_further(box):
    """Later bars may have been CATCHUP (never prepared), so `<` must promote."""
    box.prepare("t1", BAR1, PATH, payload("t1"))
    box.recover(last_processed_bar_ms=BAR3)
    assert box.get("t1").status == READY


def test_recovery_window_a_keeps_prepared_and_does_not_delete(box):
    """The regression this design exists for.

    Deleting here would lose the signal: on restart the bar may be reclassified
    CATCHUP, which the bridge suppresses, so nothing would regenerate it.
    """
    box.prepare("t2", BAR2, PATH, payload("t2"))
    assert box.recover(last_processed_bar_ms=BAR1) == {"promoted": 0, "kept": 1}
    row = box.get("t2")
    assert row is not None, "window-A PREPARED row must never be deleted"
    assert row.status == PREPARED


def test_recovery_with_no_durable_bar_keeps_everything(box):
    box.prepare("t1", BAR1, PATH, payload("t1"))
    assert box.recover(last_processed_bar_ms=None) == {"promoted": 0, "kept": 1}
    assert box.get("t1").status == PREPARED


def test_recovery_deletes_nothing_ever(box):
    box.prepare("t1", BAR1, PATH, payload("t1"))
    box.prepare("t3", BAR3, PATH, payload("t3"))
    box.recover(last_processed_bar_ms=BAR1)
    assert box.get("t1") is not None
    assert box.get("t3") is not None
    assert sum(box.stats().values()) == 2


# ------------------------------------------------------------------ #
# skipped-orphan detection
# ------------------------------------------------------------------ #

def test_skipped_orphan_is_held_not_deleted(box):
    box.prepare("t1", BAR1, PATH, payload("t1"))
    held = box.detect_skipped_orphans(durable_bar_epoch_ms=BAR3)
    assert held == ["t1"]
    row = box.get("t1")
    assert row.status == HELD
    assert "fetch window" in (row.held_reason or "")


def test_skipped_orphan_detection_excludes_the_current_bar(box):
    box.prepare("t2", BAR2, PATH, payload("t2"))
    assert box.detect_skipped_orphans(BAR3, exclude_event_key="t2") == []
    assert box.get("t2").status == PREPARED


def test_skipped_orphan_detection_ignores_future_and_non_prepared(box):
    box.prepare("t1", BAR1, PATH, payload("t1"))
    box.promote("t1")                                   # READY, not PREPARED
    box.prepare("t3", BAR3, PATH, payload("t3"))        # newer than durable bar
    assert box.detect_skipped_orphans(BAR2) == []
    assert box.get("t1").status == READY
    assert box.get("t3").status == PREPARED


# ------------------------------------------------------------------ #
# retry policy: infinite, no DEAD
# ------------------------------------------------------------------ #

def test_failure_schedules_a_retry_and_never_dies(box, poster, clock):
    poster.results = [(False, 500, None)]
    box.prepare("t1", BAR1, PATH, payload("t1"))
    box.promote("t1")
    assert box.deliver_once() == RETRY
    row = box.get("t1")
    assert row.status == READY          # never DEAD
    assert row.attempts == 1
    assert row.next_attempt_at > clock.t


def test_client_error_is_retried_not_discarded(box, poster):
    """A standing 400 is a loud fault, never licence to drop a CLOSE."""
    poster.results = [(False, 400, None)]
    box.prepare("t1", BAR1, PATH, payload("t1"))
    box.promote("t1")
    assert box.deliver_once() == RETRY
    assert box.get("t1").status == READY


def test_poster_exception_is_caught(box, poster):
    def boom(path, body):
        raise ConnectionError("dns")

    box._poster = boom
    box.prepare("t1", BAR1, PATH, payload("t1"))
    box.promote("t1")
    assert box.deliver_once() == RETRY
    assert "dns" in (box.get("t1").last_error or "")


def test_waiting_until_backoff_elapses(box, poster, clock):
    poster.results = [(False, 500, None)]
    box.prepare("t1", BAR1, PATH, payload("t1"))
    box.promote("t1")
    box.deliver_once()
    assert box.deliver_once() == WAITING
    clock.advance(BACKOFF_CAP_SECONDS + 1)
    assert box.deliver_once() == DELIVERED


def test_backoff_grows_and_caps(box, poster, clock):
    poster.results = [(False, 500, None)] * 40
    box.prepare("t1", BAR1, PATH, payload("t1"))
    box.promote("t1")
    delays = []
    for _ in range(20):
        clock.advance(BACKOFF_CAP_SECONDS + 1)
        box.deliver_once()
        delays.append(box.get("t1").next_attempt_at - clock.t)
    assert delays[0] < delays[1] < delays[2]
    assert max(delays) <= BACKOFF_CAP_SECONDS
    assert delays[-1] == BACKOFF_CAP_SECONDS


def test_success_after_failures_acks(box, poster, clock):
    poster.results = [(False, 500, None), (False, 503, None), (True, 200, None)]
    box.prepare("t1", BAR1, PATH, payload("t1"))
    box.promote("t1")
    for _ in range(3):
        clock.advance(BACKOFF_CAP_SECONDS + 1)
        box.deliver_once()
    assert box.get("t1").status == ACKED


# ------------------------------------------------------------------ #
# restart / degraded reporting
# ------------------------------------------------------------------ #

def test_rows_survive_reopen(tmp_path, clock, poster):
    db = str(tmp_path / "outbox.db")
    first = Outbox(db, poster=poster, clock=clock)
    first.prepare("t1", BAR1, PATH, payload("t1"))
    first.prepare("t2", BAR2, PATH, payload("t2"))
    first.promote("t1")
    first.close()

    second = Outbox(db, poster=poster, clock=clock)
    try:
        assert second.get("t1").status == READY
        assert second.get("t2").status == PREPARED
        assert second.get("t1").payload_dict()["bar_time"] == "t1"
    finally:
        second.close()


def test_degraded_is_none_when_healthy(box):
    assert box.degraded() is None
    box.prepare("t1", BAR1, PATH, payload("t1"))
    box.promote("t1")
    assert box.degraded() is None


def test_degraded_reports_undelivered_after_threshold(box, poster, clock):
    poster.results = [(False, 500, None)] * 20
    box.prepare("t1", BAR1, PATH, payload("t1"))
    box.promote("t1")
    for _ in range(ESCALATE_AFTER_ATTEMPTS):
        clock.advance(BACKOFF_CAP_SECONDS + 1)
        box.deliver_once()
    d = box.degraded()
    assert d is not None and d["reason"] == "undelivered"
    assert d["event_key"] == "t1"


def test_degraded_reports_held(box):
    box.prepare("t1", BAR1, PATH, payload("t1"))
    box.hold("t1", "payload divergence on recovery")
    d = box.degraded()
    assert d is not None and d["reason"] == "held"
    assert d["detail"] == "payload divergence on recovery"


def test_acked_rows_stop_blocking(box, poster):
    box.prepare("t1", BAR1, PATH, payload("t1"))
    box.promote("t1")
    box.deliver_once()
    assert box.head() is None
    assert box.degraded() is None


# ------------------------------------------------------------------ #
# canonical payload
# ------------------------------------------------------------------ #

def test_canonical_json_is_key_order_independent():
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


def test_canonical_json_distinguishes_values():
    assert canonical_json({"ml_accept": True}) != canonical_json({"ml_accept": False})


def test_canonical_json_round_trips_through_storage(box):
    p = payload("t1", ml_prob=0.5123456789012345)
    box.prepare("t1", BAR1, PATH, p)
    assert canonical_json(box.get("t1").payload_dict()) == canonical_json(p)
