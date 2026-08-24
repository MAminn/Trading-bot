// A number is the client's money only when it came from their Binance account.
//
//   node --test src/lib/wallet-display.test.ts
//
// The bug this locks down: the dashboard rendered `capital_usd + strategy P&L`
// under the label "Equity". For a brand-new user who has not connected a wallet,
// `capital_usd` is the 10,000 default of a config column — so the first thing a
// client saw after signing up was "Equity $10,000", which reads as "the platform
// is holding ten thousand dollars for me".
//
// The tests below are written from the new user's position outward: no keys, no
// telemetry, nothing read. Every one of those states must produce "no number",
// never a figure borrowed from configuration.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const { resolveWalletDisplay } = await import("./wallet-display.ts");

const DASHBOARD = "src/routes/app.dashboard.tsx";
const REPORTS = "src/routes/app.reports.tsx";
const CONFIGURE = "src/routes/app.configure.tsx";
const ENGINE = "src/routes/app.engine.tsx";

/** A heartbeat recent enough to count as live. */
const NOW = () => new Date().toISOString();
/** Older than any freshness window. */
const LONG_AGO = "2020-01-01T00:00:00.000Z";

function row(over: Record<string, unknown> = {}) {
  return {
    wallet_balance_usd: null,
    available_balance_usd: null,
    keys_present: null,
    last_heartbeat: NOW(),
    ...over,
  } as never;
}

// --------------------------------------------------------------------------
// The brand-new user — the case that produced the bug
// --------------------------------------------------------------------------

test("a brand-new user with nothing connected shows no balance", () => {
  // No keys, no executor telemetry: the exact state minutes after signup.
  const result = resolveWalletDisplay(null);
  assert.equal(result.state, "not_connected");
  // The discriminated union carries no number in this state, so there is
  // nothing a caller could render as a balance even by mistake.
  assert.equal("walletUsd" in result, false);
});

test("no telemetry but keys connected means awaiting a read, not zero", () => {
  // The client connected keys seconds ago; the executor has not completed a
  // cycle. "Not read yet" and "zero balance" are different claims.
  const result = resolveWalletDisplay(null, { keysConnected: true });
  assert.equal(result.state, "awaiting_read");
  assert.equal("walletUsd" in result, false);
});

test("the executor reporting keys_present=false is not connected", () => {
  const result = resolveWalletDisplay(row({ keys_present: false }));
  assert.equal(result.state, "not_connected");
});

test("keys_present=false outranks a stale balance still on the row", () => {
  // The client disconnected their keys. The last balance the executor read is
  // still in the column; it must not keep being shown as their money.
  const result = resolveWalletDisplay(row({ keys_present: false, wallet_balance_usd: 4200 }));
  assert.equal(result.state, "not_connected");
  assert.equal("walletUsd" in result, false);
});

test("an OFF-mode report (keys_present null) shows no balance on its own", () => {
  // The executor reports keys_present=null in OFF mode: "not yet known", which
  // is not a confirmation. Without separate evidence of connected keys this
  // must stay closed.
  const result = resolveWalletDisplay(row({ keys_present: null }));
  assert.equal(result.state, "not_connected");
});

// --------------------------------------------------------------------------
// A real reading, and only then
// --------------------------------------------------------------------------

test("a live signed read shows the real Binance balance", () => {
  const result = resolveWalletDisplay(
    row({ keys_present: true, wallet_balance_usd: 512.34, available_balance_usd: 500.1 }),
  );
  assert.equal(result.state, "connected");
  if (result.state !== "connected") return;
  assert.equal(result.walletUsd, 512.34);
  assert.equal(result.availableUsd, 500.1);
  assert.equal(result.stale, false);
});

test("a real balance of zero is shown, not hidden", () => {
  // An empty funded-then-withdrawn account genuinely holds 0. That is a read
  // result and must be displayed; the states this guards against are the ones
  // where nothing was read at all.
  const result = resolveWalletDisplay(row({ keys_present: true, wallet_balance_usd: 0 }));
  assert.equal(result.state, "connected");
  if (result.state !== "connected") return;
  assert.equal(result.walletUsd, 0);
});

test("keys present but no balance yet is awaiting a read", () => {
  const result = resolveWalletDisplay(row({ keys_present: true, wallet_balance_usd: null }));
  assert.equal(result.state, "awaiting_read");
});

test("an unreadable balance is awaiting a read, never rendered", () => {
  for (const bad of [NaN, Infinity, -Infinity, "1234", undefined, {}]) {
    const result = resolveWalletDisplay(row({ keys_present: true, wallet_balance_usd: bad }));
    assert.equal(result.state, "awaiting_read", `expected ${String(bad)} to yield no balance`);
  }
});

test("an unreadable available balance does not invalidate the wallet balance", () => {
  const result = resolveWalletDisplay(
    row({ keys_present: true, wallet_balance_usd: 100, available_balance_usd: NaN }),
  );
  assert.equal(result.state, "connected");
  if (result.state !== "connected") return;
  assert.equal(result.walletUsd, 100);
  assert.equal(result.availableUsd, null);
});

test("a stale heartbeat still shows the balance but flags it", () => {
  // Hiding it would be worse: the client would see "not connected" for an
  // account that is connected. Labelling it lets them judge the age.
  const result = resolveWalletDisplay(
    row({ keys_present: true, wallet_balance_usd: 250, last_heartbeat: LONG_AGO }),
  );
  assert.equal(result.state, "connected");
  if (result.state !== "connected") return;
  assert.equal(result.stale, true);
});

// --------------------------------------------------------------------------
// The dashboard must not reintroduce the borrowed number
// --------------------------------------------------------------------------

test("the dashboard never labels a figure plain 'Equity'", () => {
  const src = readFileSync(DASHBOARD, "utf8");
  // `label="Equity"` was the tile that rendered the config default as money.
  assert.equal(src.includes('label="Equity"'), false);
  assert.equal(src.includes(">Equity<"), false);
});

test("the dashboard headline tile reads the wallet, not the config", () => {
  const src = readFileSync(DASHBOARD, "utf8");
  assert.ok(src.includes('label="Wallet balance"'));
  assert.ok(src.includes("resolveWalletDisplay"));
  assert.ok(src.includes("Not connected"));
});

test("the strategy curve is labelled as a model, not a balance", () => {
  for (const page of [DASHBOARD, REPORTS]) {
    const src = readFileSync(page, "utf8");
    assert.ok(
      src.includes("Strategy equity curve"),
      `${page} should name the curve as the strategy's`,
    );
    assert.ok(
      src.includes("not your wallet"),
      `${page} should say plainly that the baseline is not funds`,
    );
  }
});

test("capital_usd remains the P&L scaling base, and only that", () => {
  // capital_usd is no longer an input to live ORDER SIZING — orders are sized
  // from the user's real Binance totalWalletBalance. It survives solely as the
  // notional the strategy's percentage returns are scaled by on the reporting
  // pages, which is a presentation quantity and is labelled as one.
  const src = readFileSync(DASHBOARD, "utf8");
  assert.ok(src.includes("capital_usd"));
  assert.ok(src.includes("computeMetrics"));
});

test("no page sizes an order from a configured capital figure", () => {
  // The sizing helpers take a wallet balance, never a config column. If a page
  // fed capital_usd into them it would be the old model wearing a new name.
  for (const page of [DASHBOARD, CONFIGURE, ENGINE, REPORTS]) {
    const src = readFileSync(page, "utf8");
    assert.equal(
      /(targetNotional|allocatedMargin)\([^)]*capital_usd/.test(src),
      false,
      `${page} feeds capital_usd into a sizing helper`,
    );
  }
});

test("the wallet figure is never derived from config on the dashboard", () => {
  const src = readFileSync(DASHBOARD, "utf8");
  // The old expression, in any spacing. Its absence is the regression guard:
  // a balance must come from resolveWalletDisplay and nowhere else.
  assert.equal(/const\s+equity\s*=\s*capital\s*\+/.test(src), false);
});
