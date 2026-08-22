// Who the engine acts for, and what the roster is not allowed to become.
//
//   node --test src/lib/engine-roster.test.ts
//
// The roster is the mechanism that removed the operator from onboarding, so the
// rules it encodes are the rules that decide whether a paying client's account
// gets traded. Two failure directions matter and both are asserted:
//
//   TOO NARROW — a client who signed up, connected keys and pressed Start is
//                absent from the roster, so nothing happens and no telemetry
//                explains why.
//   TOO WIDE   — a client who did NOT ask for live execution appears on it, and
//                a process starts fetching their keys and building a session
//                against their real wallet.
//
// Route shapes are read as source text, the same approach as
// executor-status-contract.test.ts: the routes import @tanstack/react-router
// and cannot be imported here.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const {
  EXECUTABLE_MODES,
  MAX_ROSTER_USERS,
  isExecutable,
  isSignalSubscriber,
  selectRoster,
  selectSignalSubscribers,
} = await import("./engine-roster.server.ts");

const ALICE = "aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa";
const BOB = "bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb";

const ROSTER_ROUTE = "src/routes/api/public/engine/users.active.ts";
const SIGNAL_ROUTE = "src/routes/api/public/engine/ingest.signal.ts";
const CREDENTIALS_ROUTE = "src/routes/api/public/engine/credentials.ts";

// --------------------------------------------------------------------------
// Execution eligibility
// --------------------------------------------------------------------------

test("a user asking for a live mode is executable", () => {
  for (const execution_mode of EXECUTABLE_MODES) {
    assert.equal(isExecutable({ user_id: ALICE, execution_mode }), true);
  }
});

test("a user at OFF is not executable", () => {
  assert.equal(isExecutable({ user_id: ALICE, execution_mode: "OFF" }), false);
});

test("a stopped user stays executable so their dashboard keeps updating", () => {
  // is_running is the kill switch, not the roster. Dropping a stopped user
  // would freeze their telemetry and show a stale heartbeat instead of
  // `kill_switch_active` — the executor still runs their session and reports
  // exactly why no order is being placed.
  assert.equal(
    isExecutable({ user_id: ALICE, execution_mode: "LIVE_TRADE", is_running: false }),
    true,
  );
});

test("demo mode is never executable", () => {
  // Demo signals are fabricated; an executor consuming them would place real
  // orders against invented data. The database CHECK forbids the combination —
  // this is the second lock, not the only one.
  assert.equal(
    isExecutable({ user_id: ALICE, execution_mode: "LIVE_TRADE", demo_mode: true }),
    false,
  );
});

test("an unknown or missing execution_mode is not executable", () => {
  // Fail closed: uncertainty about what was requested is never resolved as
  // permission. A row written before the live-controls migration has no column.
  for (const execution_mode of [
    undefined,
    null,
    "",
    "   ",
    "TESTNET_TRADE",
    "live_trade",
    "ON",
    42,
    true,
  ]) {
    assert.equal(
      isExecutable({ user_id: ALICE, execution_mode }),
      false,
      `expected ${String(execution_mode)} to be non-executable`,
    );
  }
});

test("executable modes never include a testnet mode", () => {
  // The testnet/mainnet choice is a property of the host's .env, never
  // something a web UI can select.
  for (const mode of EXECUTABLE_MODES) {
    assert.equal(mode.startsWith("TESTNET"), false);
  }
});

// --------------------------------------------------------------------------
// Roster assembly
// --------------------------------------------------------------------------

test("the roster keeps only executable users", () => {
  const { userIds } = selectRoster([
    { user_id: ALICE, execution_mode: "LIVE_TRADE" },
    { user_id: BOB, execution_mode: "OFF" },
  ]);
  assert.deepEqual(userIds, [ALICE]);
});

test("the roster deduplicates", () => {
  // Two sessions for one account would each be unaware of the other's position.
  const { userIds } = selectRoster([
    { user_id: ALICE, execution_mode: "LIVE_TRADE" },
    { user_id: ALICE, execution_mode: "LIVE_READ" },
  ]);
  assert.deepEqual(userIds, [ALICE]);
});

test("the roster rejects malformed user ids", () => {
  const { userIds } = selectRoster([
    { user_id: ALICE, execution_mode: "LIVE_TRADE" },
    { user_id: "not-a-uuid", execution_mode: "LIVE_TRADE" },
    { user_id: "", execution_mode: "LIVE_TRADE" },
    { user_id: null, execution_mode: "LIVE_TRADE" },
    { user_id: 12345, execution_mode: "LIVE_TRADE" },
  ]);
  assert.deepEqual(userIds, [ALICE]);
});

test("the roster is capped and reports the truncation", () => {
  // Each entry becomes a Binance client and a credentials fetch. An unbounded
  // roster is an unbounded number of exchange connections from one host, and an
  // operator must be told when someone is being left out rather than guessing.
  const rows = Array.from({ length: MAX_ROSTER_USERS + 5 }, (_, i) => ({
    user_id: `aaaaaaaa-1111-1111-1111-${String(i).padStart(12, "0")}`,
    execution_mode: "LIVE_TRADE",
  }));
  const { userIds, truncated } = selectRoster(rows);
  assert.equal(userIds.length, MAX_ROSTER_USERS);
  assert.equal(truncated, true);
});

test("a roster within the cap is not reported as truncated", () => {
  const { truncated } = selectRoster([{ user_id: ALICE, execution_mode: "LIVE_READ" }]);
  assert.equal(truncated, false);
});

test("an empty input yields an empty roster, not an error", () => {
  assert.deepEqual(selectRoster([]), { userIds: [], truncated: false });
});

// --------------------------------------------------------------------------
// Signal subscribers
// --------------------------------------------------------------------------

test("a running user receives signals", () => {
  assert.equal(isSignalSubscriber({ user_id: ALICE, is_running: true }), true);
});

test("a user who never pressed Start receives no signals", () => {
  for (const is_running of [undefined, null, false, "true", 1]) {
    assert.equal(
      isSignalSubscriber({ user_id: ALICE, is_running }),
      false,
      `expected ${String(is_running)} to not subscribe`,
    );
  }
});

test("a demo user receives no broadcast signals", () => {
  assert.equal(isSignalSubscriber({ user_id: ALICE, is_running: true, demo_mode: true }), false);
});

test("signal subscribers do not require a live execution mode", () => {
  // A signal_only client watches the strategy without executing it. Gating the
  // signal stream on execution_mode would blank their dashboard.
  const ids = selectSignalSubscribers([
    { user_id: ALICE, is_running: true, execution_mode: "OFF" },
  ]);
  assert.deepEqual(ids, [ALICE]);
});

test("subscribers are deduplicated and validated", () => {
  const ids = selectSignalSubscribers([
    { user_id: ALICE, is_running: true },
    { user_id: ALICE, is_running: true },
    { user_id: "nope", is_running: true },
  ]);
  assert.deepEqual(ids, [ALICE]);
});

// --------------------------------------------------------------------------
// The roster endpoint leaks nothing
// --------------------------------------------------------------------------

test("the roster route returns no key material or key metadata", () => {
  const src = readFileSync(ROSTER_ROUTE, "utf8");
  for (const forbidden of [
    "api_key",
    "api_secret",
    "encrypted",
    "last4",
    // Deliberately absent: a presence flag here would make this an endpoint for
    // enumerating which accounts hold credentials.
    "keys_present",
    "loadUserBinanceCredentials",
    "decrypt",
  ]) {
    assert.equal(src.includes(forbidden), false, `${ROSTER_ROUTE} must not reference ${forbidden}`);
  }
});

test("the roster route is read-only", () => {
  const src = readFileSync(ROSTER_ROUTE, "utf8");
  assert.ok(src.includes("GET:"));
  for (const method of ["POST:", "PUT:", "PATCH:", "DELETE:"]) {
    assert.equal(src.includes(method), false, `${ROSTER_ROUTE} must not define ${method}`);
  }
});

test("the roster route is token-protected", () => {
  const src = readFileSync(ROSTER_ROUTE, "utf8");
  assert.ok(src.includes("ENGINE_SERVICE_TOKEN"));
  assert.ok(src.includes("Unauthorized"));
  // The roster is not key material, so it stays behind the service token — and
  // must NOT be given the credentials token, which would widen that token's use.
  assert.equal(src.includes("ENGINE_CREDENTIALS_TOKEN"), false);
});

test("only the credentials route decrypts, still", () => {
  // The roster and fan-out work added two new server-side data paths; neither
  // may become a second place that produces plaintext keys.
  for (const route of [ROSTER_ROUTE, SIGNAL_ROUTE]) {
    const src = readFileSync(route, "utf8");
    assert.equal(src.includes("loadUserBinanceCredentials"), false);
    assert.equal(src.includes("crypto.server"), false);
  }
  assert.ok(readFileSync(CREDENTIALS_ROUTE, "utf8").includes("loadUserBinanceCredentials"));
});

// --------------------------------------------------------------------------
// Fan-out
// --------------------------------------------------------------------------

test("the signal route still accepts an addressed post", () => {
  const src = readFileSync(SIGNAL_ROUTE, "utf8");
  // The single-tenant worker keeps working unchanged.
  assert.ok(src.includes("if (addressed)"));
});

test("the signal route broadcasts only to subscribers", () => {
  const src = readFileSync(SIGNAL_ROUTE, "utf8");
  assert.ok(src.includes("loadSignalSubscribers"));
  // Never the execution roster: a signal_only client must still get signals.
  assert.equal(src.includes("loadExecutionRoster"), false);
});

test("broadcast delivery is idempotent", () => {
  const src = readFileSync(SIGNAL_ROUTE, "utf8");
  // The worker retries on a timeout. Without this filter a retry would deliver
  // a second copy of the same bar to every client, which the executor would
  // read as a second signal and could act on.
  assert.ok(src.includes("bar_time"));
  assert.ok(src.includes("duplicate"));
});
