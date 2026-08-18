// Phase 2: cross-field safety invariants for the live-execution controls.
//
// Run with Node's built-in test runner — no test framework dependency:
//   node --test src/lib/live-controls.test.ts
//
// These assert the app-layer rules only. The database CHECK constraints are
// the authority and are verified separately by
// supabase/verification/phase1_live_controls.sql; this layer exists so a user
// gets a clear message rather than a raw constraint violation, and it must
// never be more permissive than the database.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  LIVE_ORDER_CAP_MAX_USD,
  LIVE_ORDER_CAP_MIN_TRADE_USD,
  PRIVILEGED_CONFIG_FIELDS,
  REQUESTED_EXECUTION_MODES,
  formatViolations,
  isRequestedLiveMode,
  validateLiveState,
  type LiveState,
} from "./live-controls.ts";

const SAFE: LiveState = {
  execution_mode: "OFF",
  live_order_cap_usd: 0,
  live_allow_full_capital: false,
  sizing_mode: "allocation",
  demo_mode: false,
};

const fields = (state: Partial<LiveState>) => validateLiveState(state).map((v) => v.field);

// --- constants stay pinned to the executor's own ceilings ----------------- //

test("cap ceiling matches the executor's absolute maximum", () => {
  assert.equal(LIVE_ORDER_CAP_MAX_USD, 500);
});

test("trade floor clears the exchange minimum notional after step rounding", () => {
  assert.ok(LIVE_ORDER_CAP_MIN_TRADE_USD > 20);
});

test("the web cannot request a testnet mode", () => {
  assert.deepEqual([...REQUESTED_EXECUTION_MODES], ["OFF", "LIVE_READ", "LIVE_TRADE"]);
});

test("every column Phase 1 revoked is listed as privileged", () => {
  assert.deepEqual([...PRIVILEGED_CONFIG_FIELDS].sort(), [
    "execution_mode",
    "live_allow_full_capital",
    "live_order_cap_usd",
    "mode",
  ]);
});

// --- the safe baseline ---------------------------------------------------- //

test("the fail-closed default state is valid", () => {
  assert.deepEqual(validateLiveState(SAFE), []);
});

test("an empty patch asserts nothing", () => {
  assert.deepEqual(validateLiveState({}), []);
});

// --- invalid cap ---------------------------------------------------------- //

test("a cap above the executor ceiling is rejected", () => {
  assert.deepEqual(fields({ ...SAFE, live_order_cap_usd: 501 }), ["live_order_cap_usd"]);
});

test("a negative cap is rejected", () => {
  assert.deepEqual(fields({ ...SAFE, live_order_cap_usd: -1 }), ["live_order_cap_usd"]);
});

test("a non-finite cap is rejected rather than coerced", () => {
  for (const bad of [Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.deepEqual(fields({ ...SAFE, live_order_cap_usd: bad }), ["live_order_cap_usd"]);
  }
});

test("the ceiling itself is allowed", () => {
  assert.deepEqual(validateLiveState({ ...SAFE, live_order_cap_usd: LIVE_ORDER_CAP_MAX_USD }), []);
});

// --- LIVE_TRADE requires a workable cap ----------------------------------- //

test("LIVE_TRADE with a cap below the floor is rejected", () => {
  assert.deepEqual(fields({ ...SAFE, execution_mode: "LIVE_TRADE", live_order_cap_usd: 10 }), [
    "live_order_cap_usd",
  ]);
});

test("LIVE_TRADE with the default zero cap is rejected", () => {
  assert.deepEqual(fields({ ...SAFE, execution_mode: "LIVE_TRADE" }), ["live_order_cap_usd"]);
});

test("LIVE_TRADE at exactly the floor is accepted", () => {
  assert.deepEqual(
    validateLiveState({
      ...SAFE,
      execution_mode: "LIVE_TRADE",
      live_order_cap_usd: LIVE_ORDER_CAP_MIN_TRADE_USD,
    }),
    [],
  );
});

test("LIVE_TRADE at the production cap of 30 is accepted", () => {
  assert.deepEqual(
    validateLiveState({ ...SAFE, execution_mode: "LIVE_TRADE", live_order_cap_usd: 30 }),
    [],
  );
});

test("LIVE_READ needs no cap — it cannot place", () => {
  assert.deepEqual(
    validateLiveState({ ...SAFE, execution_mode: "LIVE_READ", live_order_cap_usd: 0 }),
    [],
  );
});

// --- full capital requires explicit consent ------------------------------- //

test("live full_capital without consent is rejected", () => {
  assert.deepEqual(
    fields({
      ...SAFE,
      execution_mode: "LIVE_TRADE",
      live_order_cap_usd: 30,
      sizing_mode: "full_capital",
      live_allow_full_capital: false,
    }),
    ["live_allow_full_capital"],
  );
});

test("live full_capital with consent is accepted", () => {
  assert.deepEqual(
    validateLiveState({
      ...SAFE,
      execution_mode: "LIVE_TRADE",
      live_order_cap_usd: 30,
      sizing_mode: "full_capital",
      live_allow_full_capital: true,
    }),
    [],
  );
});

test("full_capital off a live mode needs no consent", () => {
  assert.deepEqual(
    validateLiveState({ ...SAFE, sizing_mode: "full_capital", live_allow_full_capital: false }),
    [],
  );
});

// --- demo mode and live execution are mutually exclusive ------------------ //

test("demo mode with LIVE_TRADE is rejected", () => {
  assert.deepEqual(
    fields({ ...SAFE, execution_mode: "LIVE_TRADE", live_order_cap_usd: 30, demo_mode: true }),
    ["demo_mode"],
  );
});

test("demo mode with LIVE_READ is rejected — read-only is still live", () => {
  assert.deepEqual(fields({ ...SAFE, execution_mode: "LIVE_READ", demo_mode: true }), [
    "demo_mode",
  ]);
});

test("demo mode with OFF is fine", () => {
  assert.deepEqual(validateLiveState({ ...SAFE, demo_mode: true }), []);
});

// --- partial states: never report an unevaluable rule as satisfied -------- //

test("a demo_mode-only patch asserts nothing without the mode it pairs with", () => {
  // The handler re-runs this against the merged row, where execution_mode is
  // known; reporting "valid" here must not be mistaken for "safe".
  assert.deepEqual(validateLiveState({ demo_mode: true }), []);
});

test("merging a demo_mode-only patch over a live row is rejected", () => {
  const current = { ...SAFE, execution_mode: "LIVE_TRADE", live_order_cap_usd: 30 };
  assert.deepEqual(fields({ ...current, demo_mode: true }), ["demo_mode"]);
});

test("merging a sizing_mode-only patch over a live row is rejected", () => {
  const current = { ...SAFE, execution_mode: "LIVE_TRADE", live_order_cap_usd: 30 };
  assert.deepEqual(fields({ ...current, sizing_mode: "full_capital" }), [
    "live_allow_full_capital",
  ]);
});

// --- unknown modes fail closed -------------------------------------------- //

test("an unknown execution_mode is rejected and stops further rules", () => {
  const violations = validateLiveState({ ...SAFE, execution_mode: "TESTNET_TRADE" });
  assert.deepEqual(
    violations.map((v) => v.field),
    ["execution_mode"],
  );
});

test("an unknown mode is not silently treated as OFF", () => {
  const violations = validateLiveState({
    execution_mode: "BANANA",
    live_order_cap_usd: 9999,
    demo_mode: true,
  });
  assert.ok(violations.length > 0);
  assert.equal(violations[0].field, "execution_mode");
});

// --- multiple violations are all reported --------------------------------- //

test("independent violations are reported together", () => {
  const violations = validateLiveState({
    execution_mode: "LIVE_TRADE",
    live_order_cap_usd: 5,
    sizing_mode: "full_capital",
    live_allow_full_capital: false,
    demo_mode: true,
  });
  assert.deepEqual(violations.map((v) => v.field).sort(), [
    "demo_mode",
    "live_allow_full_capital",
    "live_order_cap_usd",
  ]);
  assert.ok(formatViolations(violations).includes(";"));
});

// --- helper --------------------------------------------------------------- //

test("isRequestedLiveMode covers both live modes and nothing else", () => {
  assert.equal(isRequestedLiveMode("LIVE_TRADE"), true);
  assert.equal(isRequestedLiveMode("LIVE_READ"), true);
  assert.equal(isRequestedLiveMode("OFF"), false);
  assert.equal(isRequestedLiveMode(null), false);
  assert.equal(isRequestedLiveMode(undefined), false);
});
