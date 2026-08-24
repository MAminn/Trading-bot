// The single sizing model, as the UI and the server validator see it.
//
// Run with Node's built-in test runner — no test framework dependency:
//   node --test src/lib/sizing.test.ts
//
// The executor computes the same product in Python Decimal against the user's
// live Binance totalWalletBalance. These tests pin the shared table of
// permitted values and the arithmetic, so the number a client is shown before
// saving is the number the executor asks Binance for.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  ALLOCATION_PCTS,
  ALLOC_MAX,
  ALLOC_MIN,
  DEFAULT_ALLOCATION_PCT,
  DEFAULT_LEVERAGE,
  LEVERAGE_MAX,
  LEVERAGE_MIN,
  LEVERAGE_STEPS,
  allocatedMargin,
  isAllocationPct,
  isLeverageStep,
  liquidationPct,
  targetNotional,
  toAllocationPct,
  toLeverageStep,
} from "./sizing.ts";

// --- the permitted value sets --------------------------------------------- //

test("allocation offers exactly the specified percentages", () => {
  assert.deepEqual(
    [...ALLOCATION_PCTS],
    [1, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
  );
  assert.equal(ALLOC_MIN, 1);
  assert.equal(ALLOC_MAX, 100);
});

test("leverage offers exactly the specified multipliers", () => {
  assert.deepEqual([...LEVERAGE_STEPS], [1, 10, 20, 30, 40, 50, 60, 70, 80, 90]);
  assert.equal(LEVERAGE_MIN, 1);
  assert.equal(LEVERAGE_MAX, 90);
});

test("allocation reaches 100% — the old 1-10% ceiling is gone", () => {
  assert.ok(isAllocationPct(100));
  assert.ok(isAllocationPct(50));
  assert.ok(isAllocationPct(20));
});

test("values between the steps are not selectable", () => {
  for (const bad of [0, 2, 3, 7, 15, 99, 101, -1, 1.5, Number.NaN]) {
    assert.equal(isAllocationPct(bad), false, `${bad} must not be a valid allocation`);
  }
  for (const bad of [0, 5, 25, 91, 100, -1, 30.5, Number.NaN]) {
    assert.equal(isLeverageStep(bad), false, `${bad} must not be a valid leverage`);
  }
});

test("defaults fail small", () => {
  assert.equal(DEFAULT_ALLOCATION_PCT, 1);
  assert.equal(DEFAULT_LEVERAGE, 1);
});

// --- snapping legacy rows -------------------------------------------------- //

test("a stored value snaps DOWN to a permitted one, never up", () => {
  assert.equal(toAllocationPct(7), 5);
  assert.equal(toAllocationPct(9), 5);
  assert.equal(toAllocationPct(29), 20);
  assert.equal(toAllocationPct(100), 100);
  assert.equal(toAllocationPct(1000), 100);
  assert.equal(toLeverageStep(35), 30);
  assert.equal(toLeverageStep(9), 1);
  assert.equal(toLeverageStep(125), 90);
});

test("an unreadable stored value falls back to the smallest step", () => {
  for (const bad of [null, undefined, "", "abc", Number.NaN, {}]) {
    assert.equal(toAllocationPct(bad), 1);
    assert.equal(toLeverageStep(bad), 1);
  }
  // Below the smallest step is also the smallest step, not zero.
  assert.equal(toAllocationPct(0), 1);
  assert.equal(toLeverageStep(0), 1);
});

// --- the formula ----------------------------------------------------------- //

test("the worked examples from the specification", () => {
  // $300 wallet, 10%, 1x => $30
  assert.equal(allocatedMargin(300, 10), 30);
  assert.equal(targetNotional(300, 10, 1), 30);

  // $300 wallet, 20%, 30x => $1,800
  assert.equal(allocatedMargin(300, 20), 60);
  assert.equal(targetNotional(300, 20, 30), 1800);

  // $300 wallet, 100%, 10x => $3,000
  assert.equal(allocatedMargin(300, 100), 300);
  assert.equal(targetNotional(300, 100, 10), 3000);
});

test("changing leverage does not alter the allocated margin", () => {
  const margins = LEVERAGE_STEPS.map(() => allocatedMargin(300, 20));
  assert.deepEqual(new Set(margins), new Set([60]));
  // ... and the notional scales exactly with leverage.
  for (const lev of LEVERAGE_STEPS) {
    assert.equal(targetNotional(300, 20, lev), 60 * lev);
  }
});

test("changing allocation does not alter leverage", () => {
  // Leverage is a free parameter of the function: the same value produces the
  // same multiplier at every allocation.
  for (const pct of ALLOCATION_PCTS) {
    assert.equal(targetNotional(300, pct, 30) / allocatedMargin(300, pct), 30);
  }
});

test("every allocation x leverage pair is reachable", () => {
  // 120 combinations, none forbidden by the other's value. The old design
  // permitted exactly ten pairs.
  const seen = new Set<string>();
  for (const pct of ALLOCATION_PCTS) {
    for (const lev of LEVERAGE_STEPS) {
      seen.add(`${pct}:${lev}`);
      assert.ok(targetNotional(1000, pct, lev) > 0);
    }
  }
  assert.equal(seen.size, ALLOCATION_PCTS.length * LEVERAGE_STEPS.length);
  assert.equal(seen.size, 120);
});

test("no wallet reading means no size, not a fabricated one", () => {
  for (const bad of [0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.equal(allocatedMargin(bad, 100), 0);
    assert.equal(targetNotional(bad, 100, 90), 0);
  }
});

test("liquidation distance is 1/leverage", () => {
  assert.equal(liquidationPct(1), 100);
  assert.equal(liquidationPct(10), 10);
  assert.ok(Math.abs(liquidationPct(90) - 1.111) < 0.001);
  assert.equal(liquidationPct(0), 0);
});

// --- nothing of the old model survives ------------------------------------- //

test("the sizing module exports no full-capital concept", () => {
  const src = readFileSync(new URL("./sizing.ts", import.meta.url), "utf8");
  for (const banned of [
    "full_capital",
    "fullCapital",
    "SIZING_MODES",
    "SizingMode",
    "LEVERAGE_BY_ALLOC",
    "accountSize",
    "capitalFromAllocation",
  ]) {
    assert.ok(!src.includes(banned), `${banned} still present in sizing.ts`);
  }
});

test("the Configure page has no account-size input or full-capital control", () => {
  const src = readFileSync(new URL("../routes/app.configure.tsx", import.meta.url), "utf8");
  for (const banned of [
    "account_size_usd",
    "Account size",
    "full_capital",
    "allowFullCapital",
    "live_allow_full_capital",
    "sizing_mode",
    "capital_usd",
  ]) {
    assert.ok(!src.includes(banned), `${banned} still present in app.configure.tsx`);
  }
});

// --- the Configure page reads a real wallet, never a config column --------- //

test("Configure sources its wallet figure from the authenticated user's own executor status", () => {
  const src = readFileSync(new URL("../routes/app.configure.tsx", import.meta.url), "utf8");

  // The chain must be: useExecutorStatus() -> resolveWalletDisplay() -> walletUsd.
  // useExecutorStatus reads executor_status under RLS, so it can only ever
  // return the signed-in user's own row; the executor writes wallet_balance_usd
  // there from a signed read of THAT user's Binance totalWalletBalance.
  assert.ok(src.includes("useExecutorStatus()"), "must read executor telemetry");
  assert.ok(src.includes("resolveWalletDisplay("), "must go through the wallet-display rule");
  assert.ok(
    /walletUsd\s*=\s*wallet\.state === "connected" \? wallet\.walletUsd : null/.test(src),
    "walletUsd must come from a connected wallet reading, or be null",
  );

  // The wallet figure must never be reconstructed from configuration.
  assert.ok(!src.includes("capital_usd"), "capital_usd must not appear on Configure");
  assert.ok(!src.includes("account_size"), "no manual account size may appear");
  assert.equal(
    /walletUsd\s*=[^\n]*config/.test(src),
    false,
    "the wallet must not be derived from the config row",
  );
});

test("the wallet input is read-only — no control can set a balance", () => {
  const src = readFileSync(new URL("../routes/app.configure.tsx", import.meta.url), "utf8");
  // Exactly one <input> may remain on this page, and it is not a balance: the
  // two sizing controls are sliders/buttons and the wallet is display-only.
  const inputs = src.match(/<input\b/g) ?? [];
  assert.equal(inputs.length, 0, "Configure must have no free-text numeric inputs left");
});

// --- capital_usd is contained ---------------------------------------------- //

test("capital_usd reaches no sizing path and no executor field", () => {
  for (const file of [
    "../lib/sizing.ts",
    "../lib/engine.functions.ts",
    "../routes/app.configure.tsx",
    "../routes/app.engine.tsx",
  ]) {
    const src = readFileSync(new URL(file, import.meta.url), "utf8");
    assert.ok(!src.includes("capital_usd"), `capital_usd still present in ${file}`);
  }
});

test("the executor config endpoint serves no balance-shaped column", () => {
  const src = readFileSync(
    new URL("../routes/api/public/engine/config.ts", import.meta.url),
    "utf8",
  );
  for (const banned of [
    '"account_size_usd"',
    '"max_notional_usd"',
    '"sizing_mode"',
    '"live_allow_full_capital"',
    '"live_order_cap_usd"',
  ]) {
    assert.ok(!src.includes(banned), `${banned} still served to the executor`);
  }
  // capital_usd is the one legacy passthrough, and it is commented as such.
  assert.ok(src.includes("LEGACY"), "the legacy passthrough must be marked");
});
