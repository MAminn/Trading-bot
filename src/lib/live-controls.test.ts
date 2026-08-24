// Cross-field safety invariants for the live-execution controls.
//
// Run with Node's built-in test runner — no test framework dependency:
//   node --test src/lib/live-controls.test.ts
//
// These assert the app-layer rules only. The database CHECK constraints are
// the authority and are verified separately by
// supabase/verification/single_sizing_model.sql; this layer exists so a user
// gets a clear message rather than a raw constraint violation, and it must
// never be more permissive than the database.
//
// What is deliberately NOT here any more: a per-order dollar cap. Order size is
// the client's own wallet balance x allocation x leverage, and a control able to
// reduce that number without changing the configuration that produced it is a
// second sizing model, not a safety control. Every rule below is binary — it
// permits or refuses a MODE, never a size.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  LIVE_STATE_INPUTS,
  PRIVILEGED_CONFIG_FIELDS,
  REQUESTED_EXECUTION_MODES,
  formatViolations,
  isRequestedLiveMode,
  validateLiveState,
  type LiveState,
} from "./live-controls.ts";

const SAFE: LiveState = {
  execution_mode: "OFF",
  demo_mode: false,
};

const fields = (state: Partial<LiveState>) => validateLiveState(state).map((v) => v.field);

// --- no dollar cap exists in this layer ----------------------------------- //

test("the live state carries no per-order dollar cap", () => {
  assert.deepEqual([...LIVE_STATE_INPUTS].sort(), ["demo_mode", "execution_mode"]);
});

test("no cap constant is exported", async () => {
  const mod = await import("./live-controls.ts");
  assert.equal("LIVE_ORDER_CAP_MAX_USD" in mod, false);
  assert.equal("LIVE_ORDER_CAP_MIN_TRADE_USD" in mod, false);
  assert.equal("LIVE_CONTROL_TUPLE" in mod, false);
});

test("no cap survives in the module source", () => {
  const src = readFileSync(new URL("./live-controls.ts", import.meta.url), "utf8");
  assert.ok(!src.includes("live_order_cap"), "live_order_cap still present");
  assert.ok(!src.includes("LIVE_ORDER_CAP"), "LIVE_ORDER_CAP still present");
});

test("a cap value passed in is ignored, not honoured", () => {
  // A caller carrying the retired field must not resurrect a rule from it.
  assert.deepEqual(
    validateLiveState({
      ...SAFE,
      execution_mode: "LIVE_TRADE",
      live_order_cap_usd: 0,
    } as never),
    [],
  );
});

test("LIVE_TRADE no longer requires a cap of any size", () => {
  // The old rule refused LIVE_TRADE unless a cap of at least $25 was set.
  assert.deepEqual(validateLiveState({ ...SAFE, execution_mode: "LIVE_TRADE" }), []);
});

// --- constants ------------------------------------------------------------ //

test("the web cannot request a testnet mode", () => {
  assert.deepEqual([...REQUESTED_EXECUTION_MODES], ["OFF", "LIVE_READ", "LIVE_TRADE"]);
});

test("every column Phase 1 revoked is listed as privileged", () => {
  assert.deepEqual([...PRIVILEGED_CONFIG_FIELDS].sort(), ["execution_mode", "mode"]);
});

// --- the safe baseline ---------------------------------------------------- //

test("the fail-closed default state is valid", () => {
  assert.deepEqual(validateLiveState(SAFE), []);
});

test("an empty patch asserts nothing", () => {
  assert.deepEqual(validateLiveState({}), []);
});

test("LIVE_READ is accepted — it cannot place", () => {
  assert.deepEqual(validateLiveState({ ...SAFE, execution_mode: "LIVE_READ" }), []);
});

// --- no full-capital consent field remains -------------------------------- //

test("live_allow_full_capital is no longer part of the live state", () => {
  // There is one sizing model, so there is no second, uncapped path needing a
  // consent switch. Passing the retired field must not resurrect a rule.
  assert.deepEqual(
    validateLiveState({
      ...SAFE,
      execution_mode: "LIVE_TRADE",
      live_allow_full_capital: false,
      sizing_mode: "full_capital",
    } as never),
    [],
  );
});

// --- demo mode and live execution are mutually exclusive ------------------ //

test("demo mode with LIVE_TRADE is rejected", () => {
  assert.deepEqual(fields({ ...SAFE, execution_mode: "LIVE_TRADE", demo_mode: true }), [
    "demo_mode",
  ]);
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
  const current = { ...SAFE, execution_mode: "LIVE_TRADE" };
  assert.deepEqual(fields({ ...current, demo_mode: true }), ["demo_mode"]);
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
  const violations = validateLiveState({ execution_mode: "BANANA", demo_mode: true });
  assert.ok(violations.length > 0);
  assert.equal(violations[0].field, "execution_mode");
});

// --- formatting ----------------------------------------------------------- //

test("violations render as one readable message", () => {
  const violations = validateLiveState({ execution_mode: "LIVE_TRADE", demo_mode: true });
  assert.equal(violations.length, 1);
  assert.ok(formatViolations(violations).includes("demo mode"));
});

// --- helper --------------------------------------------------------------- //

test("isRequestedLiveMode covers both live modes and nothing else", () => {
  assert.equal(isRequestedLiveMode("LIVE_TRADE"), true);
  assert.equal(isRequestedLiveMode("LIVE_READ"), true);
  assert.equal(isRequestedLiveMode("OFF"), false);
  assert.equal(isRequestedLiveMode(null), false);
  assert.equal(isRequestedLiveMode(undefined), false);
});
