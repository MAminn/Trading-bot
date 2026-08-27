"""Real Binance accounting: the arithmetic, and the promise that it cannot trade.

Two things are under test here and they matter for different reasons.

The first is money. A customer is going to read "Net P&L +$11.66" and believe
it, so every figure has to be Binance's own: realizedPnl from the closing fills,
`commission` from every fill of both orders, FUNDING_FEE income from the window
the position was actually open. Nothing is a fee percentage, nothing is a rate,
and anything that cannot be established exactly comes back INCOMPLETE rather
than as a plausible-looking number.

The second is safety. This module runs against a live Binance account with the
client's keys while the trading engine is stopped. The tests below assert that
it has no way to place an order, change leverage, change margin type or close a
position — not by policy, but because the code paths do not exist.

Nothing here reaches a network.
"""

import inspect
import io
import logging
import re
from decimal import Decimal
from pathlib import Path

import pytest

import accounting_sync
from accounting_sync import (
    BINANCE_READ_ENDPOINTS,
    AccountingError,
    BinanceAccountingClient,
    attribute_funding,
    build_episode_payload,
    classify_close_source,
    commission_total,
    dec_str,
    format_accounting_log,
    net_pnl_of,
    price_trade,
    reconstruct_episodes,
    split_episode,
    total_qty,
    weighted_avg_price,
)

ALICE = "aaaaaaaa-1111-1111-1111-111111111111"
BOB = "bbbbbbbb-2222-2222-2222-222222222222"
SYMBOL = "ETHUSDT"

# 2026-08-01T00:00:00Z, and eight hours later. Real millisecond stamps, because
# funding attribution is a comparison against them.
T_ENTRY = 1785110400000
T_EXIT = T_ENTRY + 8 * 3600 * 1000


def order(intent, side="LONG", oid="1", symbol=SYMBOL):
    return {"intent": intent, "side": side, "binance_order_id": oid, "symbol": symbol}


def fill(order_id, qty, price, commission, *, side="BUY", realized="0",
         t=T_ENTRY, asset="USDT", fid=None):
    return {
        "id": fid or f"{order_id}-{qty}-{price}-{t}",
        "orderId": order_id,
        "qty": qty,
        "price": price,
        "commission": commission,
        "commissionAsset": asset,
        "realizedPnl": realized,
        "side": side,
        "time": t,
    }


class RecordingWriter:
    """Stands in for the app endpoint, recording what would have been upserted."""

    dry_run = False

    def __init__(self, sink):
        self.sink = sink

    def write(self, payload):
        self.sink.append(payload)
        return {"net_pnl_usd": accounting_sync.dec_str(
            accounting_sync.net_pnl_of(payload) or Decimal(0))}


def funding_event(income, t, tran_id, asset="USDT"):
    return {"income": income, "time": t, "tranId": tran_id, "asset": asset, "symbol": SYMBOL}


def build(entry_fills, exit_fills, *, side="LONG", funding=(), consumed=None, user=ALICE,
          close_source="HELIX", exit_orders=1):
    """Price one trade straight from its fills, bypassing episode reconstruction.

    Used by the money tests so they can present fill shapes the position walk
    would never build — a mismatched quantity, a fill pointing the wrong way —
    and assert that the pricing refuses them.
    """
    return price_trade(
        user, SYMBOL, side, "100", "200",
        entry_fills, exit_fills, close_source, exit_orders,
        list(funding), set() if consumed is None else consumed,
    )


# --------------------------------------------------------------------------- #
# 1. one entry fill + one exit fill
# --------------------------------------------------------------------------- #

def test_single_fill_each_side_is_priced_from_binance_alone():
    payload = build(
        [fill("100", "0.5", "3000", "0.6", side="BUY", t=T_ENTRY)],
        [fill("200", "0.5", "3050", "0.61", side="SELL", realized="25", t=T_EXIT)],
    )
    assert payload["accounting_status"] == "COMPLETE"
    assert payload["entry_fill_count"] == 1
    assert payload["exit_fill_count"] == 1
    assert payload["qty"] == "0.5"
    assert payload["entry_avg_price"] == "3000"
    assert payload["exit_avg_price"] == "3050"
    assert payload["gross_pnl_usd"] == "25"
    assert payload["entry_commission_usd"] == "0.6"
    assert payload["exit_commission_usd"] == "0.61"
    # gross - commission + funding
    assert net_pnl_of(payload) == Decimal("25") - Decimal("1.21")


def test_payload_carries_the_binance_order_ids_and_times():
    payload = build(
        [fill("100", "1", "3000", "1.2", side="BUY", t=T_ENTRY)],
        [fill("200", "1", "3010", "1.2", side="SELL", realized="10", t=T_EXIT)],
    )
    assert payload["open_binance_order_id"] == "100"
    assert payload["close_binance_order_id"] == "200"
    assert payload["entry_time"] == accounting_sync._z(T_ENTRY)
    assert payload["exit_time"] == accounting_sync._z(T_EXIT)
    # UTC, and marked as such, so a customer's exports cannot drift by a timezone.
    assert payload["entry_time"].endswith("Z")
    assert payload["exit_time"].endswith("Z")


# --------------------------------------------------------------------------- #
# 2. multiple entry fills
# --------------------------------------------------------------------------- #

def test_multiple_entry_fills_are_quantity_weighted_not_averaged():
    """A MARKET order that walks the book must not report the mean of its prices.

    0.1 @ 3000 and 0.9 @ 4000 is an average entry of 3900, not 3500. The naive
    mean would understate the entry by $400 on a trade the client really did.
    """
    entry = [
        fill("100", "0.1", "3000", "0.03", side="BUY", t=T_ENTRY, fid="e1"),
        fill("100", "0.9", "4000", "0.36", side="BUY", t=T_ENTRY + 50, fid="e2"),
    ]
    payload = build(entry, [fill("200", "1.0", "4100", "0.41", side="SELL", realized="90", t=T_EXIT)])
    assert payload["accounting_status"] == "COMPLETE"
    assert Decimal(payload["entry_avg_price"]) == Decimal("3900")
    assert payload["entry_fill_count"] == 2
    # Commission is summed across BOTH entry fills, not taken from one of them.
    assert Decimal(payload["entry_commission_usd"]) == Decimal("0.39")


def test_entry_time_is_the_first_of_several_fills():
    entry = [
        fill("100", "0.5", "3000", "0.3", side="BUY", t=T_ENTRY + 900, fid="e2"),
        fill("100", "0.5", "3001", "0.3", side="BUY", t=T_ENTRY, fid="e1"),
    ]
    payload = build(entry, [fill("200", "1.0", "3100", "0.6", side="SELL", realized="50", t=T_EXIT)])
    assert payload["entry_time"] == accounting_sync._z(T_ENTRY)


# --------------------------------------------------------------------------- #
# 3. multiple exit fills
# --------------------------------------------------------------------------- #

def test_multiple_exit_fills_aggregate_pnl_price_and_commission():
    exits = [
        fill("200", "0.4", "3100", "0.248", side="SELL", realized="40", t=T_EXIT, fid="x1"),
        fill("200", "0.6", "3200", "0.384", side="SELL", realized="120", t=T_EXIT + 30, fid="x2"),
    ]
    payload = build([fill("100", "1.0", "3000", "0.6", side="BUY", t=T_ENTRY)], exits)
    assert payload["exit_fill_count"] == 2
    # Every closing fill's realizedPnl contributes; taking only the last would
    # report $120 on a trade that made $160.
    assert Decimal(payload["gross_pnl_usd"]) == Decimal("160")
    assert Decimal(payload["exit_avg_price"]) == Decimal("3160")
    assert Decimal(payload["exit_commission_usd"]) == Decimal("0.632")


def test_exit_time_is_the_last_of_several_fills():
    exits = [
        fill("200", "0.5", "3100", "0.3", side="SELL", realized="50", t=T_EXIT, fid="x1"),
        fill("200", "0.5", "3100", "0.3", side="SELL", realized="50", t=T_EXIT + 5000, fid="x2"),
    ]
    payload = build([fill("100", "1.0", "3000", "0.6", side="BUY", t=T_ENTRY)], exits)
    assert payload["exit_time"] == accounting_sync._z(T_EXIT + 5000)


# --------------------------------------------------------------------------- #
# 4 & 5. winning and losing trades
# --------------------------------------------------------------------------- #

def test_winning_trade_reports_a_net_smaller_than_its_gross():
    payload = build(
        [fill("100", "1", "3000", "0.42", side="BUY", t=T_ENTRY)],
        [fill("200", "1", "3012.5", "0.42", side="SELL", realized="12.50", t=T_EXIT)],
    )
    assert Decimal(payload["gross_pnl_usd"]) == Decimal("12.50")
    net = net_pnl_of(payload)
    assert net == Decimal("11.66")
    # The commission is a real deduction: net must be strictly below gross.
    assert net < Decimal(payload["gross_pnl_usd"])


def test_losing_trade_reports_a_net_worse_than_its_gross():
    """The SHORT from the brief, to the last decimal place."""
    payload = build(
        [fill("100", "1", "3000", "0.44887517", side="SELL", t=T_ENTRY)],
        [fill("200", "1", "3007.4241", "0.45258722", side="BUY", realized="-7.42410000", t=T_EXIT)],
        side="SHORT",
    )
    assert payload["gross_pnl_usd"] == "-7.42410000"
    assert payload["entry_commission_usd"] == "0.44887517"
    assert payload["exit_commission_usd"] == "0.45258722"
    assert net_pnl_of(payload) == Decimal("-8.32556239")
    # A loss is made WORSE by commission, never softened by it.
    assert net_pnl_of(payload) < Decimal(payload["gross_pnl_usd"])


def test_a_gross_win_can_be_a_net_loss():
    """Fees decide the outcome, and the accounting must say so.

    +$0.50 gross against $0.84 of commission is a losing trade for the client.
    Reporting it as a win would be reporting the strategy's result as theirs.
    """
    payload = build(
        [fill("100", "1", "3000", "0.42", side="BUY", t=T_ENTRY)],
        [fill("200", "1", "3000.5", "0.42", side="SELL", realized="0.50", t=T_EXIT)],
    )
    assert Decimal(payload["gross_pnl_usd"]) > 0
    assert net_pnl_of(payload) < 0


# --------------------------------------------------------------------------- #
# 6. commission aggregation
# --------------------------------------------------------------------------- #

def test_commission_is_summed_across_every_fill_of_both_orders():
    entry = [fill("100", "0.25", "3000", "0.15", side="BUY", fid=f"e{i}") for i in range(4)]
    exits = [fill("200", "0.5", "3100", "0.31", side="SELL", realized="50", t=T_EXIT, fid=f"x{i}")
             for i in range(2)]
    payload = build(entry, exits)
    assert Decimal(payload["entry_commission_usd"]) == Decimal("0.60")
    assert Decimal(payload["exit_commission_usd"]) == Decimal("0.62")


def test_commission_is_never_derived_from_a_fee_percentage():
    """Two fills of identical notional charged different amounts.

    A percentage model cannot produce this — maker/taker, VIP tier and rebates
    all move the number — so the only correct source is Binance's own figure.
    """
    entry = [
        fill("100", "1", "3000", "1.20", side="BUY", fid="e1"),
        fill("100", "1", "3000", "0.60", side="BUY", fid="e2"),
    ]
    total, reason = commission_total(entry)
    assert reason is None
    assert total == Decimal("1.80")


def test_commission_total_of_no_fills_is_zero_not_an_error():
    assert commission_total([]) == (Decimal(0), None)


# --------------------------------------------------------------------------- #
# 7. gross - commission + funding = net
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "gross,entry_c,exit_c,funding",
    [
        ("12.50", "0.42", "0.42", "0"),
        ("-7.42410000", "0.44887517", "0.45258722", "0"),
        ("100", "1.5", "1.5", "-2.25"),
        ("0", "0.5", "0.5", "3.75"),
    ],
)
def test_net_is_exactly_gross_minus_commission_plus_funding(gross, entry_c, exit_c, funding):
    payload = {
        "accounting_status": "COMPLETE",
        "gross_pnl_usd": gross,
        "entry_commission_usd": entry_c,
        "exit_commission_usd": exit_c,
        "funding_usd": funding,
    }
    expected = Decimal(gross) - Decimal(entry_c) - Decimal(exit_c) + Decimal(funding)
    assert net_pnl_of(payload) == expected


def test_the_identity_holds_through_a_full_payload_build():
    payload = build(
        [fill("100", "2", "3000", "1.80", side="BUY", t=T_ENTRY)],
        [fill("200", "2", "3050", "1.83", side="SELL", realized="100", t=T_EXIT)],
        funding=[funding_event("-0.55", T_ENTRY + 3600_000, "f1")],
    )
    gross = Decimal(payload["gross_pnl_usd"])
    commission = Decimal(payload["entry_commission_usd"]) + Decimal(payload["exit_commission_usd"])
    assert net_pnl_of(payload) == gross - commission + Decimal(payload["funding_usd"])


def test_the_database_owns_the_derived_columns():
    """commission_usd and net_pnl_usd are never sent, so they cannot be forged."""
    payload = build(
        [fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY)],
        [fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT)],
    )
    assert "commission_usd" not in payload
    assert "net_pnl_usd" not in payload

    migration = (
        Path(__file__).resolve().parents[1]
        / "supabase/migrations/20260828001000_real_binance_trade_accounting.sql"
    ).read_text(encoding="utf-8")
    assert "commission_usd numeric GENERATED ALWAYS AS" in migration
    assert "net_pnl_usd numeric GENERATED ALWAYS AS" in migration
    assert "(gross_pnl_usd - entry_commission_usd - exit_commission_usd + funding_usd) STORED" in migration


# --------------------------------------------------------------------------- #
# funding
# --------------------------------------------------------------------------- #

def test_funding_inside_the_position_window_is_attributed_to_it():
    total, count, reason = attribute_funding(
        [funding_event("-1.25", T_ENTRY + 3600_000, "f1")], T_ENTRY, T_EXIT, set()
    )
    assert (total, count, reason) == (Decimal("-1.25"), 1, None)


def test_funding_outside_the_window_belongs_to_no_trade():
    """Funding charged while the account is FLAT is nobody's trade cost."""
    events = [
        funding_event("-9.99", T_ENTRY - 60_000, "before"),
        funding_event("-9.99", T_EXIT + 60_000, "after"),
    ]
    total, count, reason = attribute_funding(events, T_ENTRY, T_EXIT, set())
    assert (total, count, reason) == (Decimal(0), 0, None)


def test_one_funding_payment_can_never_reach_two_trades():
    """The double-count this design exists to prevent.

    The same event list is offered to two overlapping windows. The first trade
    consumes it; the second must see nothing, or the client is charged twice for
    one payment Binance made once.
    """
    events = [funding_event("-1.25", T_ENTRY + 3600_000, "f1")]
    consumed: set[str] = set()
    first = attribute_funding(events, T_ENTRY, T_EXIT, consumed)
    second = attribute_funding(events, T_ENTRY, T_EXIT, consumed)
    assert first == (Decimal("-1.25"), 1, None)
    assert second == (Decimal(0), 0, None)


def test_funding_received_keeps_binances_positive_sign():
    total, count, _ = attribute_funding(
        [funding_event("0.80", T_ENTRY + 10, "f1")], T_ENTRY, T_EXIT, set()
    )
    assert total == Decimal("0.80")
    assert count == 1


def test_several_funding_events_in_one_position_are_summed():
    events = [
        funding_event("-1.00", T_ENTRY + 1000, "f1"),
        funding_event("-0.50", T_ENTRY + 2000, "f2"),
        funding_event("0.25", T_ENTRY + 3000, "f3"),
    ]
    total, count, _ = attribute_funding(events, T_ENTRY, T_EXIT, set())
    assert total == Decimal("-1.25")
    assert count == 3


def test_a_trade_with_no_funding_reports_zero_not_unknown():
    payload = build(
        [fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY)],
        [fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT)],
    )
    assert payload["funding_usd"] == "0"
    assert payload["funding_event_count"] == 0


# --------------------------------------------------------------------------- #
# 10. incomplete / missing fills fail safely
# --------------------------------------------------------------------------- #

def test_a_trade_with_no_fills_at_all_is_not_reported():
    """Outside the lookback window: nothing is known, so nothing is claimed.

    Under the fill-driven model this is decided by the position walk rather than
    by the pricer — a trade with no fills in the window produces no episode, so
    there is nothing to report. The pricer itself, reached directly, still
    refuses rather than inventing a zero.
    """
    assert reconstruct_episodes([], {"100"}) == []
    assert build([], [])["incomplete_reason"] == "missing_entry_fills"


def test_missing_exit_fills_is_incomplete_not_zero():
    payload = build([fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY)], [])
    assert payload["accounting_status"] == "INCOMPLETE"
    assert payload["incomplete_reason"] == "missing_exit_fills"
    # The money columns are absent entirely. A zero here would render as
    # "this trade cost you nothing", which is a claim we cannot make.
    for key in ("gross_pnl_usd", "entry_commission_usd", "exit_commission_usd", "funding_usd"):
        assert key not in payload


def test_missing_entry_fills_is_incomplete():
    payload = build([], [fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT)])
    assert payload["incomplete_reason"] == "missing_entry_fills"


def test_a_partial_close_is_incomplete_rather_than_mispriced():
    payload = build(
        [fill("100", "1.0", "3000", "0.6", side="BUY", t=T_ENTRY)],
        [fill("200", "0.4", "3100", "0.24", side="SELL", realized="40", t=T_EXIT)],
    )
    assert payload["accounting_status"] == "INCOMPLETE"
    assert payload["incomplete_reason"] == "entry_exit_quantity_mismatch"


def test_fills_pointing_the_wrong_way_are_incomplete():
    payload = build(
        [fill("100", "1", "3000", "0.6", side="SELL", t=T_ENTRY)],  # a LONG entry that sold
        [fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT)],
    )
    assert payload["incomplete_reason"] == "unexpected_fill_direction"


def test_an_open_that_realised_old_pnl_is_incomplete():
    """A flip, not a flat-to-open entry, so the close's realizedPnl is not ours."""
    payload = build(
        [fill("100", "1", "3000", "0.6", side="BUY", realized="-15", t=T_ENTRY)],
        [fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT)],
    )
    assert payload["incomplete_reason"] == "opening_order_realized_pnl"


def test_an_incomplete_trade_still_names_the_customer_and_the_orders():
    payload = build([fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY)], [])
    assert payload["user_id"] == ALICE
    assert payload["open_binance_order_id"] == "100"
    assert payload["close_binance_order_id"] == "200"


def test_a_single_unpriceable_trade_does_not_abort_the_others():
    """The regression this replaces: one BNB fee used to raise and kill the run.

    Every subsequent trade would then be missing from the customer's history
    with no indication anything had been dropped.
    """
    good = build(
        [fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY)],
        [fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT)],
    )
    bad = build(
        [fill("100", "1", "3000", "0.002", side="BUY", asset="BNB", t=T_ENTRY)],
        [fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT)],
    )
    assert good["accounting_status"] == "COMPLETE"
    assert bad["accounting_status"] == "INCOMPLETE"


# --------------------------------------------------------------------------- #
# 11. non-USDT commission is never silently treated as USD
# --------------------------------------------------------------------------- #

def test_bnb_commission_is_refused_not_converted():
    """0.002 BNB is not $0.002, and we have no trustworthy rate here.

    Passing the raw number through would understate the client's costs by a
    factor of several hundred.
    """
    total, reason = commission_total([fill("100", "1", "3000", "0.002", asset="BNB")])
    assert total is None
    assert reason == "non_usdt_commission_asset:BNB"


def test_a_bnb_fee_makes_the_whole_trade_incomplete():
    payload = build(
        [fill("100", "1", "3000", "0.002", side="BUY", asset="BNB", t=T_ENTRY)],
        [fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT)],
    )
    assert payload["accounting_status"] == "INCOMPLETE"
    assert payload["incomplete_reason"] == "non_usdt_commission_asset:BNB"
    assert "entry_commission_usd" not in payload
    assert "gross_pnl_usd" not in payload


def test_one_non_usdt_fill_among_many_usdt_fills_still_refuses():
    entry = [
        fill("100", "0.5", "3000", "0.30", side="BUY", fid="e1"),
        fill("100", "0.5", "3000", "0.0001", side="BUY", asset="BNB", fid="e2"),
    ]
    total, reason = commission_total(entry)
    assert total is None
    assert reason.startswith("non_usdt_commission_asset")


def test_a_missing_commission_asset_is_refused_rather_than_assumed_usdt():
    total, reason = commission_total([{"qty": "1", "price": "3000", "commission": "0.6"}])
    assert total is None
    assert reason == "non_usdt_commission_asset:unknown"


def test_non_usdt_funding_is_refused_not_converted():
    total, count, reason = attribute_funding(
        [funding_event("-0.001", T_ENTRY + 10, "f1", asset="BNB")], T_ENTRY, T_EXIT, set()
    )
    assert total is None
    assert reason == "non_usdt_funding_asset:BNB"


def test_refused_funding_does_not_consume_the_event():
    """An unattributable event stays in the pool, so a later fix can still see it."""
    consumed: set[str] = set()
    attribute_funding(
        [funding_event("-0.001", T_ENTRY + 10, "f1", asset="BNB")], T_ENTRY, T_EXIT, consumed
    )
    assert consumed == set()


# --------------------------------------------------------------------------- #
# 8. idempotency
# --------------------------------------------------------------------------- #

def test_the_same_closed_trade_produces_an_identical_payload_every_run():
    entry = [fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY)]
    exits = [fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT)]
    events = [funding_event("-0.25", T_ENTRY + 10, "f1")]
    first = build(entry, exits, funding=events, consumed=set())
    second = build(entry, exits, funding=events, consumed=set())
    assert first == second


def test_the_upsert_key_is_the_closing_binance_order():
    """One closed Binance trade is one row, whatever the sync does afterwards."""
    root = Path(__file__).resolve().parents[1]
    migration = (
        root / "supabase/migrations/20260828001000_real_binance_trade_accounting.sql"
    ).read_text(encoding="utf-8")
    endpoint = (
        root / "src/routes/api/public/engine/accounting.trade.ts"
    ).read_text(encoding="utf-8")
    assert "UNIQUE (user_id, close_binance_order_id)" in migration
    assert 'onConflict: "user_id,close_binance_order_id"' in endpoint
    assert ".upsert(" in endpoint
    assert ".insert(" not in endpoint


def test_repeated_fill_pages_are_deduplicated_by_trade_id():
    """Adjacent 7-day windows share an edge; a shared fill must not count twice."""
    calls = []

    class Fake:
        def user_trades(self, symbol, start_ms, end_ms):
            calls.append((start_ms, end_ms))
            # The same Binance trade id, returned by two adjacent windows.
            return [fill("100", "1", "3000", "0.6", fid="4815162342")]

    rows = accounting_sync.fetch_recent_fills(Fake(), SYMBOL, days=21)
    assert len(calls) > 1
    assert len(rows) == 1


def test_repeated_funding_pages_are_deduplicated_by_tran_id():
    class Fake:
        def funding(self, symbol, start_ms, end_ms):
            return [funding_event("-0.25", T_ENTRY, "f1")]

    assert len(accounting_sync.fetch_funding_events(Fake(), SYMBOL, days=21)) == 1


# --------------------------------------------------------------------------- #
# 9. no cross-user accounting leakage
# --------------------------------------------------------------------------- #

def test_the_payload_carries_the_user_it_was_built_for():
    a = build([fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY)],
              [fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT)], user=ALICE)
    b = build([fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY)],
              [fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT)], user=BOB)
    assert a["user_id"] == ALICE
    assert b["user_id"] == BOB


def test_rls_lets_a_customer_read_only_their_own_executed_trades():
    migration = (
        Path(__file__).resolve().parents[1]
        / "supabase/migrations/20260828001000_real_binance_trade_accounting.sql"
    ).read_text(encoding="utf-8")
    assert "ALTER TABLE public.executed_trades ENABLE ROW LEVEL SECURITY" in migration
    assert "FOR SELECT TO authenticated" in migration
    assert "USING (auth.uid() = user_id)" in migration
    # Read-only for the customer: no write policy, and no write grant.
    assert "FOR INSERT TO authenticated" not in migration
    assert "FOR UPDATE TO authenticated" not in migration
    assert "FOR DELETE TO authenticated" not in migration
    assert "GRANT SELECT ON public.executed_trades TO authenticated;" in migration
    assert "GRANT ALL ON public.executed_trades TO service_role;" in migration
    assert "TO anon" not in migration


def test_the_order_feed_is_scoped_to_the_requested_user():
    route = (
        Path(__file__).resolve().parents[1]
        / "src/routes/api/public/engine/accounting.orders.ts"
    ).read_text(encoding="utf-8")
    assert '.eq("user_id", parsed.user_id)' in route
    assert "process.env.ENGINE_SERVICE_TOKEN" in route


def test_one_users_sync_never_sends_another_users_id(monkeypatch):
    posted = []

    class FakeCred:
        present = True

        class credentials:
            api_key = "K"
            api_secret = "S"

    monkeypatch.setattr(
        accounting_sync, "UserCredentialsClient",
        lambda *a, **k: type("C", (), {"fetch": lambda self: FakeCred()})(),
    )
    monkeypatch.setattr(BinanceAccountingClient, "sync_clock", lambda self: None)
    monkeypatch.setattr(
        accounting_sync, "_get_orders",
        lambda base, token, user_id, symbol: [order("OPEN", "LONG", "100"), order("CLOSE", "LONG", "200")],
    )
    monkeypatch.setattr(
        accounting_sync, "fetch_recent_fills",
        lambda c, s, d: [
            fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY),
            fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT),
        ],
    )
    monkeypatch.setattr(accounting_sync, "fetch_funding_events", lambda c, s, d: [])
    accounting_sync.sync_user("http://app", "svc", "cred", ALICE, SYMBOL, 30,
                              RecordingWriter(posted))
    assert posted and all(p["user_id"] == ALICE for p in posted)


# --------------------------------------------------------------------------- #
# read-only: this process cannot trade
# --------------------------------------------------------------------------- #

def test_the_binance_client_has_exactly_one_verb():
    """No post, put, delete or patch exists on the client at all."""
    methods = {n for n, _ in inspect.getmembers(BinanceAccountingClient, inspect.isfunction)}
    for forbidden in ("_post", "_put", "_delete", "_patch", "post", "put", "delete"):
        assert forbidden not in methods
    assert methods == {"__init__", "_timestamp_ms", "_get", "sync_clock", "user_trades", "funding"}


def test_only_read_endpoints_are_reachable():
    assert BINANCE_READ_ENDPOINTS == ("/fapi/v1/time", "/fapi/v1/userTrades", "/fapi/v1/income")
    client = BinanceAccountingClient("K", "S")
    for blocked in ("/fapi/v1/order", "/fapi/v1/leverage", "/fapi/v1/marginType",
                    "/fapi/v1/allOpenOrders", "/fapi/v1/positionSide/dual"):
        with pytest.raises(AccountingError, match="refusing non-accounting"):
            client._get(blocked, signed=True)


def test_the_module_names_no_trading_endpoint_anywhere():
    source = Path(accounting_sync.__file__).read_text(encoding="utf-8")
    for blocked in ("/fapi/v1/order", "/fapi/v1/leverage", "/fapi/v1/marginType",
                    "/fapi/v1/batchOrders", "/fapi/v1/countdownCancelAll"):
        assert blocked not in source


def test_the_module_imports_nothing_from_the_trading_path():
    """Importing it cannot start, configure or nudge an executor.

    Checked against the IMPORT statements rather than the file text, so the
    module docstring stays free to name the modules it is isolated from.
    """
    source = Path(accounting_sync.__file__).read_text(encoding="utf-8")
    imports = [
        line.strip() for line in source.splitlines()
        if re.match(r"^\s*(import |from \S+ import )", line)
    ]
    forbidden = ("main", "signal_consumer", "user_session", "risk_guard",
                 "reconciler", "live_controls", "binance_client", "executor_status",
                 "multi_tenant")
    for line in imports:
        for module in forbidden:
            assert not re.search(rf"\b{module}\b", line), f"{line!r} reaches trading code"
    # The one local dependency, and it only reads keys.
    assert "from user_credentials import UserCredentialsClient" in source


def test_no_trading_module_imports_the_accounting_sync():
    """The isolation has to hold from the other side too."""
    executor_dir = Path(accounting_sync.__file__).parent
    # accounting_loop.py is the accounting RUNNER and is expected to import it;
    # it is not a trading module and imports none.
    accounting_own = {"accounting_sync.py", "accounting_loop.py"}
    for path in executor_dir.glob("*.py"):
        if path.name.startswith("test_") or path.name in accounting_own:
            continue
        assert "accounting_sync" not in path.read_text(encoding="utf-8"), path.name


# --------------------------------------------------------------------------- #
# logging carries no secrets
# --------------------------------------------------------------------------- #

def test_the_log_line_has_the_required_shape():
    payload = build(
        [fill("100", "1", "3000", "0.44887517", side="SELL", t=T_ENTRY)],
        [fill("200", "1", "3007", "0.45258722", side="BUY", realized="-7.42410000", t=T_EXIT)],
        side="SHORT",
    )
    line = format_accounting_log(payload)
    assert line.startswith("BINANCE ACCOUNTING |")
    for field in ("user=", "side=SHORT", "gross_pnl_usd=-7.42410000",
                  "entry_commission_usd=0.44887517", "exit_commission_usd=0.45258722",
                  "commission_usd=0.90146239", "funding_usd=0",
                  "net_pnl_usd=-8.32556239"):
        assert field in line


def test_the_logged_total_agrees_with_its_parts():
    payload = build(
        [fill("100", "2", "3000", "1.11", side="BUY", t=T_ENTRY)],
        [fill("200", "2", "3100", "1.22", side="SELL", realized="200", t=T_EXIT)],
        funding=[funding_event("-3.5", T_ENTRY + 10, "f1")],
    )
    line = format_accounting_log(payload)
    # The lookbehind matters: "commission_usd=" is a suffix of both
    # "entry_commission_usd=" and "exit_commission_usd=", and matching one of
    # those would let a wrong total pass this test.
    commission = Decimal(re.search(r"(?<![a-z_])commission_usd=(-?[\d.]+)", line).group(1))
    net = Decimal(re.search(r"net_pnl_usd=(-?[\d.]+)", line).group(1))
    assert commission == Decimal("2.33")
    assert net == Decimal("200") - commission + Decimal("-3.5")


def test_an_incomplete_trade_logs_its_reason_and_no_figures():
    payload = build(
        [fill("100", "1", "3000", "0.002", side="BUY", asset="BNB", t=T_ENTRY)],
        [fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT)],
    )
    line = format_accounting_log(payload)
    assert "status=INCOMPLETE" in line
    assert "reason=non_usdt_commission_asset:BNB" in line
    assert "net_pnl_usd=" not in line


def test_no_credential_ever_reaches_the_log(monkeypatch, caplog):
    api_key = "ACCTKEY_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    api_secret = "ACCTSECRET_bbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    class Cred:
        present = True

        class credentials:
            pass

    Cred.credentials.api_key = api_key
    Cred.credentials.api_secret = api_secret
    Cred.credentials.last4 = "aaaa"

    monkeypatch.setattr(
        accounting_sync, "UserCredentialsClient",
        lambda *a, **k: type("C", (), {"fetch": lambda self: Cred()})(),
    )
    monkeypatch.setattr(BinanceAccountingClient, "sync_clock", lambda self: None)
    monkeypatch.setattr(
        accounting_sync, "_get_orders",
        lambda base, token, user_id, symbol: [order("OPEN", "LONG", "100"), order("CLOSE", "LONG", "200")],
    )
    monkeypatch.setattr(
        accounting_sync, "fetch_recent_fills",
        lambda c, s, d: [
            fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY),
            fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT),
        ],
    )
    monkeypatch.setattr(accounting_sync, "fetch_funding_events", lambda c, s, d: [])

    with caplog.at_level(logging.DEBUG):
        accounting_sync.sync_user("http://app", "svc-token", "cred-token", ALICE, SYMBOL, 30,
                                  RecordingWriter([]))
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert blob
    for secret in (api_key, api_secret, "svc-token", "cred-token"):
        assert secret not in blob


def test_a_binance_error_body_is_not_echoed():
    """Binance error bodies can quote the request, and the request is signed."""
    source = Path(accounting_sync.__file__).read_text(encoding="utf-8")
    assert 'raise AccountingError(f"Binance GET {path} -> HTTP {r.status_code}")' in source


# --------------------------------------------------------------------------- #
# dry run
# --------------------------------------------------------------------------- #

def test_dry_run_computes_and_logs_but_writes_nothing(monkeypatch, caplog):
    class Cred:
        present = True

        class credentials:
            api_key = "K"
            api_secret = "S"

    monkeypatch.setattr(
        accounting_sync, "UserCredentialsClient",
        lambda *a, **k: type("C", (), {"fetch": lambda self: Cred()})(),
    )
    monkeypatch.setattr(BinanceAccountingClient, "sync_clock", lambda self: None)
    monkeypatch.setattr(
        accounting_sync, "_get_orders",
        lambda base, token, user_id, symbol: [order("OPEN", "LONG", "100"), order("CLOSE", "LONG", "200")],
    )
    monkeypatch.setattr(
        accounting_sync, "fetch_recent_fills",
        lambda c, s, d: [
            fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY),
            fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT),
        ],
    )
    monkeypatch.setattr(accounting_sync, "fetch_funding_events", lambda c, s, d: [])

    writer = accounting_sync.DryRunWriter(stream=io.StringIO())
    with caplog.at_level(logging.INFO):
        summary = accounting_sync.sync_user(
            "http://app", "svc", "cred", ALICE, SYMBOL, 30, writer
        )
    assert summary["complete"] == 1
    assert len(writer.written) == 1
    assert "BINANCE ACCOUNTING |" in "\n".join(r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def test_helix_order_ids_ignores_other_symbols_and_unsent_orders():
    orders = [
        order("OPEN", "LONG", "1", symbol="BTCUSDT"),
        {"intent": "OPEN", "side": "LONG", "binance_order_id": None, "symbol": SYMBOL},
        order("OPEN", "LONG", "9"), order("CLOSE", "LONG", "10"),
    ]
    opens, closes = accounting_sync.helix_order_ids(orders, SYMBOL)
    assert opens == {"9"}
    assert closes == {"10"}


def test_weighted_average_of_no_quantity_is_none_not_a_crash():
    assert weighted_avg_price([]) is None
    assert weighted_avg_price([fill("100", "0", "3000", "0")]) is None


def test_total_qty_sums_exactly_in_decimal():
    fills = [fill("100", "0.1", "3000", "0", fid=f"f{i}") for i in range(3)]
    assert total_qty(fills) == Decimal("0.3")


def test_decimal_strings_never_use_scientific_notation():
    """Postgres numeric accepts it, but a customer-facing CSV should not carry it."""
    assert dec_str(Decimal("0.00000001")) == "0.00000001"
    assert dec_str(Decimal("1E-8")) == "0.00000001"
    assert dec_str(Decimal("0")) == "0"


def test_every_money_field_is_sent_as_a_string():
    payload = build(
        [fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY)],
        [fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT)],
    )
    for key in ("qty", "entry_avg_price", "exit_avg_price", "gross_pnl_usd",
                "entry_commission_usd", "exit_commission_usd", "funding_usd"):
        assert isinstance(payload[key], str), key


def test_a_truncated_fill_page_refuses_rather_than_undercounting():
    """1000 rows means we cannot know which fills we did not see."""
    class Fake(BinanceAccountingClient):
        def __init__(self):
            pass

        def _get(self, path, params=None, *, signed=False):
            return [{"id": i} for i in range(accounting_sync.MAX_ROWS_PER_WINDOW)]

    with pytest.raises(AccountingError, match="refusing incomplete accounting"):
        Fake().user_trades(SYMBOL, 0, 1)


# =========================================================================== #
# EXTERNAL AND MANUAL CLOSES
#
# The gap this section exists for: accounting used to pair engine_orders OPEN
# with engine_orders CLOSE. A client who closed a Helix position by hand in the
# Binance app produced real fills, real commission and real realised P&L, and no
# CLOSE row — so the trade was invisible to them forever, and the next Helix
# OPEN silently discarded the unmatched one.
#
# Trades are now rebuilt from the FILLS. The app's order log decides only
# whether a position was ours, never what it earned.
# =========================================================================== #

HELIX_OPENS = {"100"}
HELIX_CLOSES = {"200"}


def episodes_of(fills, opens=HELIX_OPENS):
    return reconstruct_episodes(fills, opens)


def account(fills, *, opens=HELIX_OPENS, closes=HELIX_CLOSES, funding=(), consumed=None,
            user=ALICE):
    """Run the real path: fills in, reportable payloads out."""
    seen = set() if consumed is None else consumed
    out = []
    for ep in reconstruct_episodes(fills, opens):
        payload = build_episode_payload(user, SYMBOL, ep, opens, closes, list(funding), seen)
        if payload is not None:
            out.append(payload)
    return out


def test_bot_open_and_bot_close_is_one_helix_trade():
    fills = [
        fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
        fill("200", "1", "3012.5", "0.6", side="SELL", realized="12.5", t=T_EXIT, fid="2"),
    ]
    (payload,) = account(fills)
    assert payload["accounting_status"] == "COMPLETE"
    assert payload["close_source"] == "HELIX"
    assert payload["open_binance_order_id"] == "100"
    assert payload["close_binance_order_id"] == "200"
    assert net_pnl_of(payload) == Decimal("11.3")


def test_bot_open_and_manual_binance_close_is_still_accounted():
    """The regression. Helix opened it, the client closed it in the Binance app.

    There is no CLOSE engine_order — order 999 is one the executor never sent —
    and the trade must still reach the customer's history with its real fees.
    """
    fills = [
        fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
        fill("999", "1", "3012.5", "0.6", side="SELL", realized="12.5", t=T_EXIT, fid="2"),
    ]
    (payload,) = account(fills)
    assert payload["accounting_status"] == "COMPLETE"
    assert payload["close_source"] == "EXTERNAL"
    assert payload["close_binance_order_id"] == "999"
    assert Decimal(payload["gross_pnl_usd"]) == Decimal("12.5")
    assert Decimal(payload["exit_commission_usd"]) == Decimal("0.6")
    assert net_pnl_of(payload) == Decimal("11.3")


def test_an_exchange_side_stop_is_accounted_the_same_way():
    """A stop that fires on Binance is an external close like any other."""
    fills = [
        fill("100", "1", "3000", "0.6", side="SELL", t=T_ENTRY, fid="1"),
        fill("77", "1", "3050", "0.61", side="BUY", realized="-50", t=T_EXIT, fid="2"),
    ]
    (payload,) = account(fills)
    assert payload["side"] == "SHORT"
    assert payload["close_source"] == "EXTERNAL"
    assert net_pnl_of(payload) == Decimal("-50") - Decimal("1.21")


def test_a_position_closed_in_stages_by_both_is_mixed_and_fully_priced():
    """Client closes half by hand, Helix closes the rest. One trade, all fees."""
    fills = [
        fill("100", "1.0", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
        fill("888", "0.4", "3100", "0.24", side="SELL", realized="40", t=T_EXIT, fid="2"),
        fill("200", "0.6", "3200", "0.38", side="SELL", realized="120", t=T_EXIT + 60, fid="3"),
    ]
    (payload,) = account(fills)
    assert payload["accounting_status"] == "COMPLETE"
    assert payload["close_source"] == "MIXED"
    assert payload["exit_order_count"] == 2
    assert payload["exit_fill_count"] == 2
    # Both closing fills' realised P&L, and both their commissions.
    assert Decimal(payload["gross_pnl_usd"]) == Decimal("160")
    assert Decimal(payload["exit_commission_usd"]) == Decimal("0.62")
    # Keyed on the order that finally flattened the position.
    assert payload["close_binance_order_id"] == "200"


def test_an_unrelated_manual_trade_is_never_attributed_to_helix():
    """The client's own trade, opened and closed by them. Not our business.

    It must not appear in their Helix history at all — not as a trade, and not
    as an INCOMPLETE row implying Helix had something to do with it.
    """
    fills = [
        fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
        fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_ENTRY + 100, fid="2"),
        # Entirely theirs: order 555 opened it, order 556 closed it.
        fill("555", "5", "3000", "3.0", side="BUY", t=T_ENTRY + 200, fid="3"),
        fill("556", "5", "3100", "3.1", side="SELL", realized="500", t=T_EXIT, fid="4"),
    ]
    payloads = account(fills)
    assert len(payloads) == 1
    assert payloads[0]["open_binance_order_id"] == "100"
    assert all(p["open_binance_order_id"] != "555" for p in payloads)
    # And the client's $500 is nowhere in the customer's Helix P&L.
    assert Decimal(payloads[0]["gross_pnl_usd"]) == Decimal("10")


def test_a_manual_trade_between_two_bot_trades_does_not_merge_them():
    fills = [
        fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
        fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_ENTRY + 10, fid="2"),
        fill("555", "2", "3000", "1.2", side="BUY", t=T_ENTRY + 20, fid="3"),
        fill("556", "2", "2990", "1.2", side="SELL", realized="-20", t=T_ENTRY + 30, fid="4"),
        fill("101", "1", "3000", "0.6", side="BUY", t=T_ENTRY + 40, fid="5"),
        fill("201", "1", "3020", "0.6", side="SELL", realized="20", t=T_EXIT, fid="6"),
    ]
    payloads = account(fills, opens={"100", "101"}, closes={"200", "201"})
    assert [p["open_binance_order_id"] for p in payloads] == ["100", "101"]
    assert [Decimal(p["gross_pnl_usd"]) for p in payloads] == [Decimal("10"), Decimal("20")]


def test_an_external_order_adding_to_a_bot_position_fails_safe():
    """Ambiguous: the close realises P&L on size Helix did not put on."""
    fills = [
        fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
        fill("555", "1", "3010", "0.6", side="BUY", t=T_ENTRY + 10, fid="2"),
        fill("200", "2", "3100", "1.2", side="SELL", realized="190", t=T_EXIT, fid="3"),
    ]
    (payload,) = account(fills)
    assert payload["accounting_status"] == "INCOMPLETE"
    assert payload["incomplete_reason"] == "external_order_added_to_position"
    # No figure survives an ambiguous attribution.
    for key in ("gross_pnl_usd", "entry_commission_usd", "exit_commission_usd", "funding_usd"):
        assert key not in payload


def test_a_position_still_open_is_not_reported_as_a_trade():
    fills = [fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1")]
    assert account(fills) == []
    assert episodes_of(fills)[0].closed is False


def test_a_position_open_at_the_end_of_the_window_does_not_swallow_the_one_before():
    fills = [
        fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
        fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_ENTRY + 10, fid="2"),
        fill("101", "1", "3020", "0.6", side="BUY", t=T_EXIT, fid="3"),
    ]
    payloads = account(fills, opens={"100", "101"})
    assert len(payloads) == 1
    assert payloads[0]["close_binance_order_id"] == "200"


def test_fills_before_the_first_bot_open_are_discarded():
    """A window that starts mid-position must not seed the walk at the wrong size.

    Here the client was already long 2 when the window opened. Anchoring on the
    first Helix OPEN drops those leading fills instead of letting them shift
    every later episode boundary.
    """
    fills = [
        fill("444", "2", "2900", "1.2", side="SELL", realized="80", t=T_ENTRY - 5000, fid="0"),
        fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
        fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT, fid="2"),
    ]
    (payload,) = account(fills)
    assert Decimal(payload["gross_pnl_usd"]) == Decimal("10")
    assert payload["entry_fill_count"] == 1


def test_no_bot_open_in_the_window_reports_nothing():
    fills = [
        fill("555", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
        fill("556", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT, fid="2"),
    ]
    assert reconstruct_episodes(fills, set()) == []
    assert account(fills, opens=set()) == []


def test_close_source_classification():
    assert classify_close_source(["200"], HELIX_CLOSES) == "HELIX"
    assert classify_close_source(["999"], HELIX_CLOSES) == "EXTERNAL"
    assert classify_close_source(["200", "999"], HELIX_CLOSES) == "MIXED"


def test_split_episode_puts_reducing_fills_on_the_exit_side():
    fills = [
        fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
        fill("999", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT, fid="2"),
    ]
    entry, exits, side = split_episode(fills)
    assert side == "LONG"
    assert [f["orderId"] for f in entry] == ["100"]
    assert [f["orderId"] for f in exits] == ["999"]


def test_a_short_episode_is_detected_from_its_first_fill():
    fills = [
        fill("100", "1", "3000", "0.6", side="SELL", t=T_ENTRY, fid="1"),
        fill("200", "1", "2990", "0.6", side="BUY", realized="10", t=T_EXIT, fid="2"),
    ]
    (payload,) = account(fills)
    assert payload["side"] == "SHORT"
    assert payload["accounting_status"] == "COMPLETE"


# =========================================================================== #
# ACCOUNTING BOUNDARIES
# =========================================================================== #

def test_funding_exactly_at_the_entry_timestamp_belongs_to_the_trade():
    total, count, reason = attribute_funding(
        [funding_event("-1.00", T_ENTRY, "f1")], T_ENTRY, T_EXIT, set()
    )
    assert (total, count, reason) == (Decimal("-1.00"), 1, None)


def test_funding_exactly_at_the_exit_timestamp_belongs_to_the_trade():
    total, count, reason = attribute_funding(
        [funding_event("-1.00", T_EXIT, "f1")], T_ENTRY, T_EXIT, set()
    )
    assert (total, count, reason) == (Decimal("-1.00"), 1, None)


def test_a_close_and_an_immediate_re_entry_do_not_both_claim_one_payment():
    """Touching windows. Inclusive endpoints make this the sharpest case.

    Trade A exits at T and trade B enters at T. A funding payment stamped T is
    inside both windows; it must be counted once, for the earlier trade.
    """
    events = [funding_event("-1.25", T_EXIT, "f1")]
    fills = [
        fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
        fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT, fid="2"),
        fill("101", "1", "3010", "0.6", side="BUY", t=T_EXIT, fid="3"),
        fill("201", "1", "3020", "0.6", side="SELL", realized="10", t=T_EXIT + 5000, fid="4"),
    ]
    a, b = account(fills, opens={"100", "101"}, closes={"200", "201"}, funding=events)
    assert Decimal(a["funding_usd"]) == Decimal("-1.25")
    assert a["funding_event_count"] == 1
    assert Decimal(b["funding_usd"]) == Decimal("0")
    assert b["funding_event_count"] == 0
    # Counted once across the pair, not once each.
    assert Decimal(a["funding_usd"]) + Decimal(b["funding_usd"]) == Decimal("-1.25")


def test_a_partial_close_that_never_flattens_is_not_reported():
    """Half closed, half still open. Not a completed trade yet."""
    fills = [
        fill("100", "1.0", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
        fill("200", "0.4", "3100", "0.24", side="SELL", realized="40", t=T_EXIT, fid="2"),
    ]
    assert account(fills) == []
    assert episodes_of(fills)[0].closed is False


def test_a_partial_close_completed_later_is_one_trade_not_two():
    fills = [
        fill("100", "1.0", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
        fill("200", "0.4", "3100", "0.24", side="SELL", realized="40", t=T_EXIT, fid="2"),
        fill("201", "0.6", "3200", "0.38", side="SELL", realized="120", t=T_EXIT + 99, fid="3"),
    ]
    payloads = account(fills, closes={"200", "201"})
    assert len(payloads) == 1
    assert payloads[0]["exit_order_count"] == 2
    assert Decimal(payloads[0]["gross_pnl_usd"]) == Decimal("160")
    assert Decimal(payloads[0]["qty"]) == Decimal("1.0")


def test_many_partial_fills_across_both_sides_aggregate_once():
    fills = (
        [fill("100", "0.25", "3000", "0.15", side="BUY", t=T_ENTRY + i, fid=f"e{i}")
         for i in range(4)]
        + [fill("200", "0.5", "3100", "0.31", side="SELL", realized="50", t=T_EXIT + i, fid=f"x{i}")
           for i in range(2)]
    )
    (payload,) = account(fills)
    assert payload["entry_fill_count"] == 4
    assert payload["exit_fill_count"] == 2
    assert Decimal(payload["qty"]) == Decimal("1.00")
    assert Decimal(payload["entry_commission_usd"]) == Decimal("0.60")
    assert Decimal(payload["exit_commission_usd"]) == Decimal("0.62")
    assert Decimal(payload["gross_pnl_usd"]) == Decimal("100")


def test_a_duplicate_fill_page_cannot_double_a_position():
    """The same fills returned twice must not build a phantom second episode."""
    dup = [
        fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
        fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT, fid="2"),
    ]

    class Fake:
        def user_trades(self, symbol, start_ms, end_ms):
            return dup

    rows = accounting_sync.fetch_recent_fills(Fake(), SYMBOL, days=21)
    assert len(rows) == 2
    payloads = account(rows)
    assert len(payloads) == 1
    assert Decimal(payloads[0]["gross_pnl_usd"]) == Decimal("10")


def test_a_gross_win_that_is_a_net_loss_survives_the_full_path():
    fills = [
        fill("100", "1", "3000", "0.42", side="BUY", t=T_ENTRY, fid="1"),
        fill("200", "1", "3000.5", "0.42", side="SELL", realized="0.50", t=T_EXIT, fid="2"),
    ]
    (payload,) = account(fills)
    assert Decimal(payload["gross_pnl_usd"]) > 0
    assert net_pnl_of(payload) < 0


def test_a_non_usdt_commission_on_an_external_close_is_still_refused():
    fills = [
        fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
        fill("999", "1", "3010", "0.002", side="SELL", realized="10", asset="BNB",
             t=T_EXIT, fid="2"),
    ]
    (payload,) = account(fills)
    assert payload["accounting_status"] == "INCOMPLETE"
    assert payload["incomplete_reason"] == "non_usdt_commission_asset:BNB"
    assert payload["close_source"] == "EXTERNAL"


def test_an_open_realising_pnl_fails_safe_through_the_full_path():
    fills = [
        fill("100", "1", "3000", "0.6", side="BUY", realized="-15", t=T_ENTRY, fid="1"),
        fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT, fid="2"),
    ]
    (payload,) = account(fills)
    assert payload["incomplete_reason"] == "opening_order_realized_pnl"


def test_an_incomplete_payload_carries_no_money_field_at_all():
    """Whatever the reason, and whether or not pricing had already run."""
    for fills in (
        [fill("100", "1", "3000", "0.002", side="BUY", asset="BNB", t=T_ENTRY, fid="1"),
         fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT, fid="2")],
        [fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
         fill("555", "1", "3010", "0.6", side="BUY", t=T_ENTRY + 5, fid="2"),
         fill("200", "2", "3100", "1.2", side="SELL", realized="190", t=T_EXIT, fid="3")],
    ):
        (payload,) = account(fills)
        assert payload["accounting_status"] == "INCOMPLETE"
        for key in accounting_sync.MONEY_FIELDS:
            assert key not in payload, key
        assert "funding_event_count" not in payload


# =========================================================================== #
# MULTI-TENANT USER DISCOVERY
# =========================================================================== #

class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def roster_with(monkeypatch, response):
    captured = {}

    class FakeSession:
        def __init__(self):
            self.headers = {}

        def get(self, url, timeout=None):
            captured["url"] = url
            captured["headers"] = dict(self.headers)
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(accounting_sync.requests, "Session", FakeSession)
    return captured


def test_production_discovery_uses_the_accounting_roster(monkeypatch):
    captured = roster_with(
        monkeypatch,
        FakeResponse(200, {"users": [{"user_id": ALICE}, {"user_id": BOB}], "count": 2}),
    )
    users = accounting_sync.resolve_users("http://app", "svc-token", override="")
    assert users == [ALICE, BOB]
    assert captured["url"] == "http://app/api/public/engine/accounting/users"
    # Service-token gated, exactly as the executor's own roster call is.
    assert captured["headers"]["Authorization"] == "Bearer svc-token"


def test_the_pinned_override_is_a_development_shortcut_only(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("the roster must not be fetched when users are pinned")

    monkeypatch.setattr(accounting_sync.AccountingRoster, "fetch", explode)
    assert accounting_sync.resolve_users("http://app", "svc", f" {ALICE}, {BOB} ") == [ALICE, BOB]


def test_a_duplicated_roster_entry_is_not_synced_twice(monkeypatch):
    roster_with(monkeypatch, FakeResponse(200, {"users": [{"user_id": ALICE}, {"user_id": ALICE}]}))
    assert accounting_sync.resolve_users("http://app", "svc", "") == [ALICE]


def test_a_rejected_roster_token_is_an_operator_error_not_an_empty_run(monkeypatch):
    """An empty roster would look like 'no customers' and write nothing, quietly."""
    roster_with(monkeypatch, FakeResponse(401))
    with pytest.raises(accounting_sync.RosterUnavailable, match="ENGINE_SERVICE_TOKEN"):
        accounting_sync.resolve_users("http://app", "svc", "")


def test_a_malformed_roster_body_is_refused(monkeypatch):
    roster_with(monkeypatch, FakeResponse(200, {"nope": 1}))
    with pytest.raises(accounting_sync.RosterUnavailable):
        accounting_sync.resolve_users("http://app", "svc", "")
    roster_with(monkeypatch, FakeResponse(200, None))
    with pytest.raises(accounting_sync.RosterUnavailable):
        accounting_sync.resolve_users("http://app", "svc", "")


def test_a_truncated_roster_is_reported_rather_than_silently_applied(monkeypatch, caplog):
    roster_with(monkeypatch, FakeResponse(200, {"users": [{"user_id": ALICE}],
                                                "truncated": True, "max": 500}))
    with caplog.at_level(logging.WARNING):
        accounting_sync.resolve_users("http://app", "svc", "")
    assert "truncated" in "\n".join(r.getMessage() for r in caplog.records)


def test_the_roster_client_never_reaches_a_trading_module():
    source = Path(accounting_sync.__file__).read_text(encoding="utf-8")
    imports = [
        line.strip() for line in source.splitlines()
        if re.match(r"^\s*(import |from \S+ import )", line)
    ]
    assert not any("multi_tenant" in line for line in imports)
    assert accounting_sync.ROSTER_PATH == "/api/public/engine/accounting/users"


# ---- per-user isolation through main() ---- #

def _stub_one_user_sync(monkeypatch, behaviour):
    """Replace sync_user with a recorder, keeping main()'s loop under test."""
    seen = []

    def fake_sync(app_base, service_token, credentials_token, user_id, symbol, days, dry_run=False):
        seen.append(user_id)
        return behaviour(user_id)

    monkeypatch.setattr(accounting_sync, "sync_user", fake_sync)
    return seen


def _run_main(monkeypatch, users, **env):
    monkeypatch.setenv("APP_API_BASE", "http://app")
    monkeypatch.setenv("ENGINE_SERVICE_TOKEN", "svc")
    monkeypatch.setenv("ENGINE_CREDENTIALS_TOKEN", "cred")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(accounting_sync, "resolve_users", lambda *a, **k: users)
    accounting_sync.main()


def test_two_users_are_processed_independently(monkeypatch):
    seen = _stub_one_user_sync(monkeypatch, lambda u: {"complete": 1})
    _run_main(monkeypatch, [ALICE, BOB])
    assert seen == [ALICE, BOB]


def test_one_users_credential_failure_does_not_stop_another(monkeypatch, caplog):
    def behaviour(user_id):
        if user_id == ALICE:
            raise accounting_sync.AccountingError("Binance GET /fapi/v1/userTrades -> HTTP 401")
        return {"complete": 1}

    seen = _stub_one_user_sync(monkeypatch, behaviour)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit):
            _run_main(monkeypatch, [ALICE, BOB])
    # Bob was still accounted for, after Alice blew up.
    assert seen == [ALICE, BOB]
    # And the failure is loud rather than swallowed.
    assert "accounting sync failed" in "\n".join(r.getMessage() for r in caplog.records)


def test_a_user_with_no_connected_keys_is_skipped_not_failed(monkeypatch, caplog):
    class NoKeys:
        present = False
        credentials = None
        blocked_reason = "missing_user_binance_keys"

    monkeypatch.setattr(
        accounting_sync, "UserCredentialsClient",
        lambda *a, **k: type("C", (), {"fetch": lambda self: NoKeys()})(),
    )

    def explode(*a, **k):
        raise AssertionError("a user without keys must not reach Binance")

    monkeypatch.setattr(BinanceAccountingClient, "sync_clock", explode)

    with caplog.at_level(logging.INFO):
        summary = accounting_sync.sync_user("http://app", "svc", "cred", ALICE, SYMBOL, 30)
    assert summary == {"episodes": 0, "complete": 0, "incomplete": 0,
                       "external_close": 0, "not_ours": 0, "still_open": 0}
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "credentials unavailable" in blob


def test_no_user_can_be_written_under_another_users_id(monkeypatch):
    posted = []

    def cred_for(app_base, token, user_id):
        class C:
            def fetch(self):
                class R:
                    present = True

                    class credentials:
                        api_key = f"KEY-{user_id}"
                        api_secret = f"SECRET-{user_id}"

                return R()

        return C()

    monkeypatch.setattr(accounting_sync, "UserCredentialsClient", cred_for)
    monkeypatch.setattr(BinanceAccountingClient, "sync_clock", lambda self: None)
    monkeypatch.setattr(
        accounting_sync, "_get_orders",
        lambda base, token, user_id, symbol: [order("OPEN", "LONG", "100"),
                                              order("CLOSE", "LONG", "200")],
    )
    monkeypatch.setattr(
        accounting_sync, "fetch_recent_fills",
        lambda c, s, d: [
            fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
            fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT, fid="2"),
        ],
    )
    monkeypatch.setattr(accounting_sync, "fetch_funding_events", lambda c, s, d: [])
    writer = RecordingWriter(posted)

    for user in (ALICE, BOB):
        accounting_sync.sync_user("http://app", "svc", "cred", user, SYMBOL, 30, writer)

    assert [p["user_id"] for p in posted] == [ALICE, BOB]
    assert len({p["user_id"] for p in posted}) == 2


def test_funding_attribution_is_not_shared_between_users(monkeypatch):
    """Alice's consumed transaction ids must not suppress Bob's funding.

    They are Binance transaction ids from different accounts and can collide.
    """
    posted = []
    events = [funding_event("-1.25", T_ENTRY + 10, "f1")]

    class Cred:
        present = True

        class credentials:
            api_key = "K"
            api_secret = "S"

    monkeypatch.setattr(
        accounting_sync, "UserCredentialsClient",
        lambda *a, **k: type("C", (), {"fetch": lambda self: Cred()})(),
    )
    monkeypatch.setattr(BinanceAccountingClient, "sync_clock", lambda self: None)
    monkeypatch.setattr(
        accounting_sync, "_get_orders",
        lambda base, token, user_id, symbol: [order("OPEN", "LONG", "100"),
                                              order("CLOSE", "LONG", "200")],
    )
    monkeypatch.setattr(
        accounting_sync, "fetch_recent_fills",
        lambda c, s, d: [
            fill("100", "1", "3000", "0.6", side="BUY", t=T_ENTRY, fid="1"),
            fill("200", "1", "3010", "0.6", side="SELL", realized="10", t=T_EXIT, fid="2"),
        ],
    )
    monkeypatch.setattr(accounting_sync, "fetch_funding_events", lambda c, s, d: list(events))
    writer = RecordingWriter(posted)

    for user in (ALICE, BOB):
        accounting_sync.sync_user("http://app", "svc", "cred", user, SYMBOL, 30, writer)

    assert len(posted) == 2
    for p in posted:
        assert Decimal(p["funding_usd"]) == Decimal("-1.25")
