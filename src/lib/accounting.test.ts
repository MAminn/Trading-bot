// Real Binance accounting, as the customer reads it.
//
//   node --test src/lib/accounting.test.ts
//
// The failure these lock down is a specific one: a client seeing a number on
// this website and believing it is their money when it is not. The site already
// showed two things that are NOT their money — the strategy's modelled return
// (net_pnl_rate x the capital_usd config column) and the executor's order log —
// so the real Binance layer has to be unmistakable, and it has to refuse to
// guess.
//
// Two kinds of test below. The arithmetic ones exercise accounting-math.ts
// directly. The UI ones read the page sources, because the property that
// matters is not "a component renders" but "no page puts a modelled figure
// under a label a client will read as real money" — and that is a fact about
// the file, checkable without a DOM.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const {
  isComplete,
  realPerformance,
  realTotalsForDay,
  closedOn,
  realTradesCsv,
  REAL_TRADE_CSV_HEADER,
  fmtCommission,
  netTone,
  incompleteLabel,
  closeSourceLabel,
  UNAVAILABLE,
  accountingAvailability,
  accountingIsUnavailable,
  ACCOUNTING_UNAVAILABLE_TITLE,
  ACCOUNTING_UNAVAILABLE_BODY,
  ACCOUNTING_UNAVAILABLE_SHORT,
} = await import("./accounting-math.ts");

type Row = import("./accounting-math.ts").ExecutedTradeRow;

const DASHBOARD = "src/routes/app.dashboard.tsx";
const HISTORY = "src/routes/app.history.tsx";
const REPORTS = "src/routes/app.reports.tsx";
const MIGRATION = "supabase/migrations/20260828001000_real_binance_trade_accounting.sql";
const TRADE_ROUTE = "src/routes/api/public/engine/accounting.trade.ts";
const ORDERS_ROUTE = "src/routes/api/public/engine/accounting.orders.ts";

const src = (p: string) => readFileSync(p, "utf8");

/**
 * Executable source only — JSX comments, block comments and line comments
 * removed.
 *
 * These files document the bugs they exist to prevent, quoting the exact
 * strings that used to be wrong. Scanning raw text would make the explanation
 * fail the test that the explanation is about.
 */
const code = (p: string) =>
  src(p)
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/[^\n]*/g, "");

/** One COMPLETE trade.
 *
 *  The two totals are DERIVED here, the way Postgres derives them, so a test
 *  cannot accidentally assert against a commission or net that disagrees with
 *  the parts it is made of. An override of either is honoured, but only
 *  explicitly — the defaults can never drift apart.
 */
function trade(over: Partial<Row> = {}): Row {
  const gross = over.gross_pnl_usd ?? 12.5;
  const entryC = over.entry_commission_usd ?? 0.42;
  const exitC = over.exit_commission_usd ?? 0.42;
  const funding = over.funding_usd ?? 0;
  const commission = entryC + exitC;
  const derived = {
    gross_pnl_usd: gross,
    entry_commission_usd: entryC,
    exit_commission_usd: exitC,
    funding_usd: funding,
    commission_usd: over.commission_usd ?? commission,
    net_pnl_usd: over.net_pnl_usd ?? gross - commission + funding,
  };
  return {
    id: Math.random().toString(36).slice(2),
    user_id: "aaaaaaaa-1111-1111-1111-111111111111",
    symbol: "ETHUSDT",
    side: "LONG",
    open_binance_order_id: "100",
    close_binance_order_id: "200",
    entry_time: "2026-08-28T09:00:00.000Z",
    exit_time: "2026-08-28T17:00:00.000Z",
    qty: 1,
    entry_avg_price: 3000,
    exit_avg_price: 3012.5,
    entry_fill_count: 1,
    exit_fill_count: 1,
    exit_order_count: 1,
    close_source: "HELIX",
    funding_event_count: 0,
    accounting_status: "COMPLETE",
    incomplete_reason: null,
    source: "BINANCE",
    synced_at: "2026-08-28T17:05:00.000Z",
    ...over,
    // Last, so the derived totals always match the components above them.
    ...derived,
  };
}

/** A trade Binance executed that we could not price. */
function incomplete(reason: string): Row {
  return {
    ...trade(),
    qty: null,
    entry_avg_price: null,
    exit_avg_price: null,
    gross_pnl_usd: null,
    entry_commission_usd: null,
    exit_commission_usd: null,
    commission_usd: null,
    funding_usd: null,
    net_pnl_usd: null,
    accounting_status: "INCOMPLETE",
    incomplete_reason: reason,
  };
}

// --------------------------------------------------------------------------
// 6 & 7. commission aggregation, and gross - commission + funding = net
// --------------------------------------------------------------------------

test("commission is the sum of the entry and exit halves", () => {
  // The authoritative sum is Postgres's, in NUMERIC — asserted separately
  // against the generated column. This fixture adds in float64, so it is
  // compared to a cent's tolerance rather than pretending JS is exact.
  const t = trade({ entry_commission_usd: 0.44887517, exit_commission_usd: 0.45258722 });
  assert.ok(Math.abs(Number(t.commission_usd) - 0.90146239) < 1e-9);
});

test("net is gross minus commission plus funding, over a whole set", () => {
  const rows = [
    trade({ gross_pnl_usd: 12.5, entry_commission_usd: 0.42, exit_commission_usd: 0.42 }),
    trade({ gross_pnl_usd: -7.4241, entry_commission_usd: 0.44887517, exit_commission_usd: 0.45258722 }),
    trade({ gross_pnl_usd: 100, entry_commission_usd: 1.5, exit_commission_usd: 1.5, funding_usd: -2.25 }),
  ];
  const p = realPerformance(rows);
  assert.ok(Math.abs(p.netPnl - (p.grossPnl - p.commission + p.funding)) < 1e-9);
});

test("the brief's worked example totals to the cent", () => {
  const p = realPerformance([
    trade({ gross_pnl_usd: 12.5, entry_commission_usd: 0.42, exit_commission_usd: 0.42 }),
  ]);
  assert.equal(p.grossPnl, 12.5);
  assert.equal(p.commission, 0.84);
  assert.equal(p.funding, 0);
  assert.equal(Number(p.netPnl.toFixed(2)), 11.66);
});

test("funding keeps Binance's sign, paid and received alike", () => {
  assert.equal(realPerformance([trade({ funding_usd: -2.25 })]).funding, -2.25);
  assert.equal(realPerformance([trade({ funding_usd: 0.8 })]).funding, 0.8);
});

test("commission never reduces a total — it is always a cost", () => {
  const p = realPerformance([trade({ gross_pnl_usd: 50 }), trade({ gross_pnl_usd: -50 })]);
  assert.ok(p.commission > 0);
  assert.ok(p.netPnl < p.grossPnl);
});

// --------------------------------------------------------------------------
// winners, losers, and win rate measured on NET
// --------------------------------------------------------------------------

test("a gross win that fees turn into a net loss counts as a loss", () => {
  // +$0.50 gross against $0.84 of commission. The client is down on this trade,
  // and counting it as a win would report the strategy's result as theirs.
  const p = realPerformance([trade({ gross_pnl_usd: 0.5 })]);
  assert.ok(p.netPnl < 0);
  assert.equal(p.wins, 0);
  assert.equal(p.losses, 1);
  assert.equal(p.winRate, 0);
});

test("wins, losses and win rate come from real net results", () => {
  const p = realPerformance([
    trade({ gross_pnl_usd: 12.5 }),
    trade({ gross_pnl_usd: 20 }),
    trade({ gross_pnl_usd: -7.4241 }),
    trade({ gross_pnl_usd: -1 }),
  ]);
  assert.equal(p.trades, 4);
  assert.equal(p.wins, 2);
  assert.equal(p.losses, 2);
  assert.equal(p.winRate, 50);
});

test("an empty set is zeroes and a 0% rate, never a division by zero", () => {
  const p = realPerformance([]);
  assert.equal(p.trades, 0);
  assert.equal(p.netPnl, 0);
  assert.equal(p.winRate, 0);
  assert.ok(Number.isFinite(p.winRate));
});

// --------------------------------------------------------------------------
// 10. incomplete accounting fails safely
// --------------------------------------------------------------------------

test("an incomplete trade is counted, and contributes to no money total", () => {
  const p = realPerformance([
    trade({ gross_pnl_usd: 12.5 }),
    incomplete("non_usdt_commission_asset:BNB"),
  ]);
  assert.equal(p.trades, 1);
  assert.equal(p.incompleteTrades, 1);
  // 0.84, from the one priceable trade. Adding the unknown one as a zero would
  // understate the client's real fees.
  assert.equal(p.commission, 0.84);
  assert.equal(Number(p.netPnl.toFixed(2)), 11.66);
});

test("a row with a COMPLETE status but a missing figure is not treated as complete", () => {
  const broken = { ...trade(), net_pnl_usd: null };
  assert.equal(isComplete(broken), false);
  assert.equal(realPerformance([broken]).trades, 0);
});

test("an incomplete trade never renders as a zero", () => {
  assert.equal(fmtCommission(null), UNAVAILABLE);
  assert.equal(fmtCommission(undefined), UNAVAILABLE);
  assert.equal(netTone(null), "muted");
});

test("every incomplete reason has plain-language wording for the customer", () => {
  assert.match(incompleteLabel("non_usdt_commission_asset:BNB"), /BNB/);
  assert.match(incompleteLabel("non_usdt_commission_asset:BNB"), /not converted/);
  assert.match(incompleteLabel("missing_exit_fills"), /not available/);
  assert.match(incompleteLabel("entry_exit_quantity_mismatch"), /do not match/);
  // An unrecognised code still says "incomplete" rather than leaking the code.
  assert.equal(incompleteLabel("something_new"), "Accounting incomplete");
  assert.equal(incompleteLabel(null), "Accounting incomplete");
});

// --------------------------------------------------------------------------
// 13. the UI shows real Commission and Net P&L, and shows them as real
// --------------------------------------------------------------------------

test("commission always displays as a deduction", () => {
  // Stored as a positive cost; the minus belongs to the display, so the stored
  // number stays "what Binance charged".
  assert.equal(fmtCommission(0.84), "−$0.84");
  assert.equal(fmtCommission(1234.5), "−$1,234.50");
  // Zero fees are zero, not "−$0.00".
  assert.equal(fmtCommission(0), "$0.00");
});

test("a profitable net is success styling and a loss is destructive", () => {
  assert.equal(netTone(11.66), "success");
  assert.equal(netTone(0), "success");
  assert.equal(netTone(-8.33), "destructive");
});

test("the dashboard shows Today Net P&L and Today Commission as real Binance", () => {
  const s = src(DASHBOARD);
  assert.match(s, /label="Today Net P&L"/);
  assert.match(s, /label="Today Commission"/);
  assert.match(s, /Real Binance · after commission/);
  assert.match(s, /Actual Binance fees/);
});

test("the dashboard's real tiles are fed by executed_trades, not by capital_usd", () => {
  const s = src(DASHBOARD);
  assert.match(s, /useExecutedTrades\(/);
  assert.match(s, /realTotalsForDay\(/);
  // The real tiles read todayReal.*; the modelled tile reads todayPnl, which is
  // the only one allowed near tradePnlUsd/capital. The exact expressions are
  // pinned by the availability tests below, which also gate them on the feed
  // state — asserted here only as "these tiles come from the real feed".
  assert.match(s, /fmtUSD\(todayReal\.netPnl, true\)/);
  assert.match(s, /fmtCommission\(todayReal\.commission\)/);
});

test("the modelled P&L is labelled as modelled wherever it appears", () => {
  const s = src(DASHBOARD);
  assert.match(s, /label="Strategy P&L \(modelled\)"/);
  assert.match(s, /label="Net P&L \(modelled\)"/);
  assert.match(s, /Strategy · modelled/);
  // The bare labels that a client would read as their money are gone.
  assert.ok(!/label="Today P&L"/.test(s), 'dashboard still has a bare "Today P&L" tile');
  assert.ok(!/label="Net P&L"/.test(s), 'dashboard still has a bare "Net P&L" stat');
});

test("no page labels a capital_usd-derived figure as a real Binance result", () => {
  for (const page of [DASHBOARD, HISTORY, REPORTS]) {
    const s = src(page);
    for (const line of s.split("\n")) {
      if (!/tradePnlUsd|metrics\.netPnl|totalPnl/.test(line)) continue;
      assert.ok(
        !/Real Binance|Actual Binance|real Binance result/i.test(line),
        `${page}: a modelled figure is presented as real — ${line.trim()}`,
      );
    }
  }
});

test("the history page has a real Binance table with every required column", () => {
  const s = src(HISTORY);
  assert.match(s, /Real Binance completed trades/);
  for (const col of ["Time", "Side", "Qty", "Entry", "Exit", "Gross P&amp;L", "Commission", "Funding", "Net P&amp;L"]) {
    assert.match(s, new RegExp(`<th[^>]*>${col}</th>`), `missing column ${col}`);
  }
});

test("the history page keeps real and modelled trades in separate tables", () => {
  const s = src(HISTORY);
  assert.match(s, /function RealBinanceTrades/);
  assert.match(s, /Strategy trades/);
  assert.match(s, /useExecutedTrades\(/);
  assert.match(s, /useTrades\(/);
  // The real table is fed only from executed_trades.
  assert.match(s, /rows=\{executed\.data \?\? \[\]\}/);
});

test("the real table styles net by sign and shows commission as a deduction", () => {
  const s = src(HISTORY);
  assert.match(s, /net >= 0 \? "text-success" : "text-destructive"/);
  assert.match(s, /text-destructive[\s\S]{0,120}fmtCommission\(Number\(t\.commission_usd\)\)/);
  // An unpriceable trade renders the word, not a figure.
  assert.match(s, /UNAVAILABLE/);
  assert.match(s, /incompleteLabel\(t\.incomplete_reason\)/);
});

test("the reports page has a separate real Binance performance section", () => {
  const s = src(REPORTS);
  assert.match(s, /Real Binance performance/);
  for (const kpi of [
    'label="Real Net P&L"',
    'label="Gross P&L"',
    'label="Total commission"',
    'label="Funding"',
    'label="Real completed trades"',
    'label="Real wins"',
    'label="Real losses"',
    'label="Real win rate"',
  ]) {
    assert.ok(s.includes(kpi), `reports is missing ${kpi}`);
  }
});

test("the reports page keeps its strategy metrics labelled modelled", () => {
  const s = src(REPORTS);
  for (const kpi of [
    'label="Net P&L (modelled)"',
    'label="Trades (modelled)"',
    'label="Win rate (modelled)"',
    'label="Profit factor (modelled)"',
    'label="Max drawdown (modelled)"',
  ]) {
    assert.ok(s.includes(kpi), `reports is missing ${kpi}`);
  }
  assert.ok(!s.includes('label="Net P&L"'), "reports still has a bare Net P&L KPI");
});

test("the real reports section is computed without capital_usd", () => {
  const s = src(REPORTS);
  const section = s.slice(s.indexOf("function RealBinancePerformance"));
  const body = section.slice(0, section.indexOf("\nfunction Kpi("));
  assert.ok(body.length > 0);
  assert.ok(!/capital/.test(body), "the real performance section touches capital_usd");
  assert.ok(!/tradePnlUsd/.test(body), "the real performance section uses a modelled P&L helper");
});

// --------------------------------------------------------------------------
// 12. strategy and real results stay separate, structurally
// --------------------------------------------------------------------------

test("the accounting module is not imported by the strategy data layer", () => {
  // One direction only: pages compose the two, the trading data layer never
  // reaches for the accounting one.
  assert.ok(!/from "\.\/accounting/.test(src("src/lib/engine.ts")));
  assert.ok(!/from "\.\/accounting/.test(src("src/lib/executor.ts")));
});

test("the accounting math imports nothing at all", () => {
  const s = src("src/lib/accounting-math.ts");
  const imports = s.split("\n").filter((l) => /^\s*import[\s{]/.test(l));
  assert.deepEqual(imports, []);
});

test("no real figure is ever derived from a strategy return or a fee rate", () => {
  // Comments are stripped first: the file explains at length what it is NOT
  // allowed to use, and naming those things in prose is the point.
  const code = src("src/lib/accounting-math.ts")
    .split("\n")
    .filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l))
    .join("\n");
  for (const forbidden of ["capital", "net_pnl_rate", "tradePnlUsd", "0.0004", "feeRate", "FEE_"]) {
    assert.ok(!code.includes(forbidden), `accounting math references ${forbidden}`);
  }
});

test("the real and modelled totals of the same day do not touch", () => {
  const day = new Date("2026-08-28T12:00:00.000Z");
  const rows = [
    trade({ exit_time: "2026-08-28T17:00:00.000Z", gross_pnl_usd: 12.5 }),
    trade({ exit_time: "2026-08-20T17:00:00.000Z", gross_pnl_usd: 999 }),
  ];
  const today = realTotalsForDay(rows, day);
  assert.equal(today.trades, 1);
  assert.equal(Number(today.netPnl.toFixed(2)), 11.66);
  assert.equal(closedOn(rows, day).length, 1);
});

// --------------------------------------------------------------------------
// exports
// --------------------------------------------------------------------------

test("the real CSV carries every column the brief asks for", () => {
  for (const col of [
    "side", "entry_time", "exit_time", "qty", "entry_avg_price", "exit_avg_price",
    "gross_pnl_usd", "entry_commission_usd", "exit_commission_usd", "commission_usd",
    "funding_usd", "net_pnl_usd", "open_binance_order_id", "close_binance_order_id",
  ]) {
    assert.ok(REAL_TRADE_CSV_HEADER.split(",").includes(col), `CSV is missing ${col}`);
  }
});

test("the real CSV writes the real numbers", () => {
  const csv = realTradesCsv([
    trade({ gross_pnl_usd: 12.5, entry_commission_usd: 0.42, exit_commission_usd: 0.42 }),
  ]);
  const row = csv.split("\n")[1].split(",");
  const col = (name: string) => row[REAL_TRADE_CSV_HEADER.split(",").indexOf(name)];
  assert.equal(col("gross_pnl_usd"), "12.5");
  assert.equal(col("commission_usd"), "0.84");
  assert.equal(col("net_pnl_usd"), "11.66");
  assert.equal(col("open_binance_order_id"), "100");
  assert.equal(col("close_binance_order_id"), "200");
});

test("an incomplete trade exports empty money cells plus its reason", () => {
  const csv = realTradesCsv([incomplete("non_usdt_commission_asset:BNB")]);
  const cols = REAL_TRADE_CSV_HEADER.split(",");
  const row = csv.split("\n")[1].split(",");
  for (const name of ["gross_pnl_usd", "commission_usd", "net_pnl_usd", "funding_usd"]) {
    // Empty, not "0" — a spreadsheet must not sum an unknown as a zero.
    assert.equal(row[cols.indexOf(name)], "", `${name} exported a value`);
  }
  assert.equal(row[cols.indexOf("accounting_status")], "INCOMPLETE");
  assert.equal(row[cols.indexOf("incomplete_reason")], "non_usdt_commission_asset:BNB");
});

test("the modelled export never calls its P&L real", () => {
  for (const page of [HISTORY, REPORTS]) {
    const s = src(page);
    assert.ok(!/["']?id,trade_id[^\n]*,pnl_usd/.test(s), `${page} exports a bare pnl_usd column`);
    assert.match(s, /modelled_pnl_usd/);
  }
});

test("the strategy and real exports are separate files", () => {
  const s = src(REPORTS);
  assert.match(s, /function exportRealCSV/);
  assert.match(s, /helix-report-binance-accounting-/);
  assert.match(s, /helix-report-strategy-modelled-/);
});

// --------------------------------------------------------------------------
// 8 & 9. idempotency and user isolation, as the schema states them
// --------------------------------------------------------------------------

test("one closed Binance trade can only ever be one row", () => {
  const m = src(MIGRATION);
  assert.match(m, /UNIQUE \(user_id, close_binance_order_id\)/);
  assert.match(src(TRADE_ROUTE), /onConflict: "user_id,close_binance_order_id"/);
});

test("the customer-facing totals are generated by the database", () => {
  const m = src(MIGRATION);
  assert.match(m, /commission_usd numeric GENERATED ALWAYS AS/);
  assert.match(m, /net_pnl_usd numeric GENERATED ALWAYS AS/);
  assert.match(m, /\(gross_pnl_usd - entry_commission_usd - exit_commission_usd \+ funding_usd\) STORED/);

  // No accepted payload field can set either total. Checked against the zod
  // schemas only — the route is free to SELECT the generated columns back, and
  // does, so that the synchroniser can cross-check what it computed.
  const route = src(TRADE_ROUTE);
  const schemas = route.slice(route.indexOf("const Base"), route.indexOf("const CORS"));
  // The lookbehind matters: "commission_usd" is a suffix of the two legitimate
  // fields "entry_commission_usd" and "exit_commission_usd".
  for (const derived of [/(?<![a-z_])commission_usd\s*:/, /(?<![a-z_])net_pnl_usd\s*:/]) {
    assert.ok(!derived.test(schemas), `the trade endpoint accepts ${derived}`);
  }
  // They ARE selected back, so the synchroniser can compare the database's
  // total against the one it computed and log any disagreement.
  const selected = route.slice(route.indexOf(".select("), route.indexOf(".single()"));
  assert.match(selected, /(?<![a-z_])commission_usd/);
  assert.match(selected, /net_pnl_usd/);
});

test("a customer reads only their own executed trades and can write none", () => {
  const m = src(MIGRATION);
  assert.match(m, /ENABLE ROW LEVEL SECURITY/);
  assert.match(m, /FOR SELECT TO authenticated\s+USING \(auth\.uid\(\) = user_id\)/);
  assert.match(m, /GRANT SELECT ON public\.executed_trades TO authenticated;/);
  assert.match(m, /GRANT ALL ON public\.executed_trades TO service_role;/);
  assert.ok(!/FOR (INSERT|UPDATE|DELETE) TO authenticated/.test(m));
  assert.ok(!/TO anon/.test(m));
});

test("both accounting endpoints require the service token", () => {
  for (const route of [TRADE_ROUTE, ORDERS_ROUTE]) {
    const s = src(route);
    assert.match(s, /process\.env\.ENGINE_SERVICE_TOKEN/);
    assert.match(s, /return unauthorized\(\)/);
  }
});

test("the order feed cannot be asked for a user other than the one it scopes to", () => {
  const s = src(ORDERS_ROUTE);
  assert.match(s, /\.eq\("user_id", parsed\.user_id\)/);
  // Read-only: no write verb anywhere in the feed.
  for (const verb of [".insert(", ".update(", ".upsert(", ".delete("]) {
    assert.ok(!s.includes(verb), `the order feed calls ${verb}`);
  }
});

test("an incomplete row cannot carry a money value past the schema", () => {
  const m = src(MIGRATION);
  assert.match(m, /executed_trades_complete_is_fully_priced/);
  assert.match(m, /executed_trades_incomplete_names_its_reason/);
  // The endpoint's INCOMPLETE branch has no money fields to send.
  const route = src(TRADE_ROUTE);
  const inc = route.slice(route.indexOf("const Incomplete"), route.indexOf("const Body"));
  for (const field of ["gross_pnl_usd", "entry_commission_usd", "funding_usd", "qty"]) {
    assert.ok(!inc.includes(field), `the INCOMPLETE payload accepts ${field}`);
  }
});

test("money crosses the wire as decimal text, never as a JSON float", () => {
  const route = src(TRADE_ROUTE);
  assert.match(route, /const Decimal = z\s*\n?\s*\.string\(\)/);
  assert.match(route, /gross_pnl_usd: Decimal/);
  assert.match(route, /entry_commission_usd: NonNegativeDecimal/);
});

// --------------------------------------------------------------------------
// no trading behaviour was touched
// --------------------------------------------------------------------------

test("nothing in the accounting layer can place or change an order", () => {
  for (const f of ["src/lib/accounting.ts", "src/lib/accounting-math.ts"]) {
    const s = src(f);
    for (const verb of [".insert(", ".update(", ".upsert(", ".delete(", "useMutation"]) {
      assert.ok(!s.includes(verb), `${f} contains a write path (${verb})`);
    }
  }
});

// --------------------------------------------------------------------------
// External and manual closes
//
// Helix opens every trade recorded here, but it does not always close them: a
// client can flatten a position by hand in the Binance app, or an exchange-side
// stop can. Those are real completed trades with real fees, and the customer
// must both see them and be able to tell how they ended.
// --------------------------------------------------------------------------

test("an externally closed trade is a real trade like any other", () => {
  const p = realPerformance([
    trade({ close_source: "EXTERNAL", gross_pnl_usd: 12.5 }),
    trade({ close_source: "HELIX", gross_pnl_usd: 12.5 }),
  ]);
  // Both count, both contribute their fees. How a position was closed changes
  // the story of the trade, never its arithmetic.
  assert.equal(p.trades, 2);
  assert.equal(p.commission, 1.68);
  assert.equal(Number(p.netPnl.toFixed(2)), 23.32);
});

test("close_source is worded for the customer, and only when it is notable", () => {
  assert.equal(closeSourceLabel("HELIX"), null);
  assert.match(String(closeSourceLabel("EXTERNAL")), /outside Helix/);
  assert.match(String(closeSourceLabel("MIXED")), /Partly closed outside Helix/);
});

test("the history table tells the customer when they closed a trade themselves", () => {
  const s = src(HISTORY);
  assert.match(s, /closeSourceLabel\(t\.close_source\)/);
});

test("the real CSV records how each position was closed and in how many orders", () => {
  const cols = REAL_TRADE_CSV_HEADER.split(",");
  assert.ok(cols.includes("close_source"));
  assert.ok(cols.includes("exit_order_count"));
  const csv = realTradesCsv([
    trade({ close_source: "EXTERNAL", exit_order_count: 2, gross_pnl_usd: 12.5 }),
  ]);
  const row = csv.split("\n")[1].split(",");
  assert.equal(row[cols.indexOf("close_source")], "EXTERNAL");
  assert.equal(row[cols.indexOf("exit_order_count")], "2");
});

test("the schema constrains close_source to the three known values", () => {
  const m = src(MIGRATION);
  assert.match(m, /close_source text NOT NULL DEFAULT 'HELIX'/);
  assert.match(m, /CHECK \(close_source IN \('HELIX', 'EXTERNAL', 'MIXED'\)\)/);
});

test("the trade endpoint will not accept an invented close_source", () => {
  const route = src(TRADE_ROUTE);
  assert.match(route, /close_source: z\.enum\(\["HELIX", "EXTERNAL", "MIXED"\]\)/);
});

test("a COMPLETE row must name at least one closing order", () => {
  // Without this a trade could be stored as fully priced while claiming nothing
  // ever closed it.
  assert.match(src(MIGRATION), /AND exit_order_count > 0/);
});

// --------------------------------------------------------------------------
// Accounting availability: three states, never two
//
// The bug: `useExecutedTrades` swallowed a query error into an empty array. A
// dropped connection, an RLS misconfiguration, a schema drift or a Supabase
// outage then rendered as "No completed Binance trades" and "Today Net P&L
// $0.00" — the app telling a client, in their own currency, that their account
// did nothing today, on the strength of a question it never got an answer to.
// --------------------------------------------------------------------------

test("a failed query is unavailable, not empty", () => {
  assert.equal(accountingAvailability({ isError: true }), "unavailable");
  assert.equal(accountingIsUnavailable({ isError: true }), true);
});

test("a successful query with zero rows is a genuine empty state", () => {
  assert.equal(accountingAvailability({ isError: false, isLoading: false }), "available");
  assert.equal(accountingIsUnavailable({ isError: false, isLoading: false }), false);
  // And an empty result really does total to zero — that part was never wrong.
  const p = realPerformance([]);
  assert.equal(p.trades, 0);
  assert.equal(p.netPnl, 0);
});

test("a first load is loading, and is not mistaken for empty", () => {
  assert.equal(accountingAvailability({ isLoading: true }), "loading");
  assert.equal(accountingAvailability({ isPending: true }), "loading");
  assert.equal(accountingIsUnavailable({ isLoading: true }), false);
});

test("an error while refetching still reads as unavailable", () => {
  // React Query keeps a failing query in a refetching state. Testing isLoading
  // first would show a spinner over a broken feed, then fall through to the
  // empty state once it cleared.
  assert.equal(accountingAvailability({ isError: true, isLoading: true }), "unavailable");
});

test("the hook rethrows a query error instead of returning an empty list", () => {
  const s = src("src/lib/accounting.ts");
  assert.match(s, /if \(error\) throw error;/);
  assert.ok(
    !/if \(error\) return \[\]/.test(s),
    "a query error is still being swallowed into an empty result",
  );
});

test("one wording for the unavailable state, shared by every page", () => {
  assert.match(ACCOUNTING_UNAVAILABLE_TITLE, /Accounting unavailable/);
  assert.match(ACCOUNTING_UNAVAILABLE_BODY, /could not be loaded/);
  assert.match(ACCOUNTING_UNAVAILABLE_BODY, /until the accounting feed recovers/);
  assert.match(ACCOUNTING_UNAVAILABLE_SHORT, /unavailable/i);
  for (const page of [DASHBOARD, HISTORY, REPORTS]) {
    assert.ok(
      /ACCOUNTING_UNAVAILABLE_(TITLE|BODY|SHORT)/.test(src(page)),
      `${page} does not render the unavailable state`,
    );
  }
});

test("the dashboard shows a dash, not a figure, when accounting is down", () => {
  const s = src(DASHBOARD);
  assert.match(s, /const accountingDown = accountingIsUnavailable\(executed\)/);
  // Both headline money tiles are gated on it.
  assert.match(s, /accountingDown \? "—" : todayReal\.trades \? fmtUSD\(todayReal\.netPnl, true\)/);
  assert.match(s, /accountingDown \? "—" : todayReal\.trades \? fmtCommission\(todayReal\.commission\)/);
  // And the trade count too, so "0 trades" is never asserted from a failure.
  assert.match(s, /value=\{accountingDown \? "—" : `\$\{todayReal\.trades\}`\}/);
});

test("the dashboard never says 'no trades today' on a failed query", () => {
  const s = code(DASHBOARD);
  // Every empty-state message must sit in a ternary whose FIRST branch is the
  // unavailable wording, so a failed feed can never reach the reassuring copy.
  const messages = ["no trades closed today", "none today"];
  let found = 0;
  for (const message of messages) {
    let at = s.indexOf(message);
    while (at !== -1) {
      found += 1;
      const preceding = s.slice(Math.max(0, at - 500), at);
      const gate = preceding.lastIndexOf("accountingDown");
      const short = preceding.lastIndexOf("ACCOUNTING_UNAVAILABLE_SHORT");
      assert.ok(
        gate !== -1 && short > gate,
        `"${message}" is not gated behind the unavailable branch`,
      );
      at = s.indexOf(message, at + 1);
    }
  }
  assert.ok(found >= 2, "the dashboard empty-state wording was not found at all");
});

test("history renders an availability state, not a loading boolean", () => {
  const s = src(HISTORY);
  assert.match(s, /state: AccountingAvailability/);
  assert.match(s, /state=\{accountingAvailability\(executed\)\}/);
  assert.match(s, /const down = state === "unavailable"/);
  // The unavailable branch is checked BEFORE the empty-trades branch.
  const table = s.slice(s.indexOf("function RealBinanceTrades"));
  assert.ok(
    table.indexOf("{down ? (") < table.indexOf("rows.length > 0 ?"),
    "history falls through to the empty state on a failed query",
  );
});

test("history does not offer a CSV built from a feed it could not read", () => {
  // A CSV of zero rows is a document asserting the client made no trades.
  assert.match(src(HISTORY), /const exportable = !down && rows\.length > 0/);
});

test("reports shows the real section as unavailable rather than as zeros", () => {
  const s = src(REPORTS);
  assert.match(s, /state: AccountingAvailability/);
  assert.match(s, /state=\{accountingState\}/);
  const section = s.slice(s.indexOf("function RealBinancePerformance"));
  assert.ok(
    section.indexOf("{down ? (") < section.indexOf("rows.length === 0 ?"),
    "reports falls through to the empty state on a failed query",
  );
  // The KPI grid — where the $0.00 totals live — is on the far side of both.
  assert.ok(section.indexOf("{down ? (") < section.indexOf('label="Real Net P&L"'));
});

test("reports keeps the strategy section working when accounting is down", () => {
  const s = src(REPORTS);
  // The modelled KPIs are computed from metrics.*, which never touches the
  // accounting feed, and are rendered outside the gated block.
  assert.match(s, /label="Net P&L \(modelled\)"/);
  const modelled = s.slice(s.indexOf('label="Net P&L (modelled)"'));
  assert.ok(!/accountingDown/.test(modelled.slice(0, 600)));
});

test("no page can export or headline a real figure from a failed feed", () => {
  const reports = src(REPORTS);
  assert.match(reports, /onClick=\{exportRealCSV\} disabled=\{accountingDown \|\| !realFiltered\.length\}/);
  // The page header counts real trades; it must not read 0 from a failure.
  assert.match(reports, /accountingDown \? "real Binance accounting unavailable"/);
});
