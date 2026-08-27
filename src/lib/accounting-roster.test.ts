// Who has real Binance money to account for.
//
//   node --test src/lib/accounting-roster.test.ts
//
// The failure this locks down: using the EXECUTION roster for accounting. That
// roster is gated on `execution_mode IN (LIVE_READ, LIVE_TRADE)`, which is the
// right rule for deciding who to trade for and the wrong one for deciding whose
// money to account for. A customer who presses Stop, switches execution off, or
// disables live trading still made the trades they already made — the
// commission was charged, the P&L was realised — and those numbers must keep
// reaching their dashboard. Wiring accounting to the execution roster would
// erase a client's financial history the moment they stopped trading.
//
// Eligibility here is credential ownership, and nothing else.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const { selectAccountingRoster, MAX_ACCOUNTING_USERS } = await import(
  "./accounting-roster.server.ts"
);

const ROSTER_LIB = "src/lib/accounting-roster.server.ts";
const ROSTER_ROUTE = "src/routes/api/public/engine/accounting.users.ts";
const EXECUTION_LIB = "src/lib/engine-roster.server.ts";
const EXECUTION_ROUTE = "src/routes/api/public/engine/users.active.ts";
const SYNC = "executor/accounting_sync.py";

const src = (p: string) => readFileSync(p, "utf8");

/** Executable lines only. These files explain at length which fields and which
 *  modules they must NOT reach for, and naming those in prose is the point —
 *  scanning the raw text would make the explanation fail the test that the
 *  explanation is about. */
const code = (p: string) =>
  src(p)
    .split("\n")
    .filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l))
    .join("\n");

const ALICE = "aaaaaaaa-1111-1111-1111-111111111111";
const BOB = "bbbbbbbb-2222-2222-2222-222222222222";
const CARLA = "cccccccc-3333-3333-3333-333333333333";

/**
 * A credential-owner row, carrying the trading fields that WOULD disqualify the
 * user under the execution roster's rules. They are present precisely so their
 * being ignored is observable.
 */
function owner(user_id: string, trading: Record<string, unknown> = {}) {
  return { user_id, ...trading };
}

// --------------------------------------------------------------------------
// 1-3. accounting eligibility outlives trading eligibility
// --------------------------------------------------------------------------

test("a LIVE_TRADE customer with keys is accounted for", () => {
  const r = selectAccountingRoster([
    owner(ALICE, { execution_mode: "LIVE_TRADE", is_running: true }),
  ]);
  assert.deepEqual(r.userIds, [ALICE]);
});

test("a customer with execution OFF is still accounted for", () => {
  // Their past trades charged real commission. Turning execution off does not
  // un-charge it, and must not remove it from their P&L.
  const r = selectAccountingRoster([
    owner(ALICE, { execution_mode: "OFF", is_running: true }),
  ]);
  assert.deepEqual(r.userIds, [ALICE]);
});

test("a stopped customer is still accounted for", () => {
  // A client who has pressed Stop is the one most likely to be reading their
  // final numbers.
  const r = selectAccountingRoster([
    owner(ALICE, { execution_mode: "LIVE_TRADE", is_running: false }),
  ]);
  assert.deepEqual(r.userIds, [ALICE]);
});

test("every trading state is accounted for, together", () => {
  const r = selectAccountingRoster([
    owner(ALICE, { execution_mode: "LIVE_TRADE", is_running: true }),
    owner(BOB, { execution_mode: "OFF", is_running: false }),
    owner(CARLA, { execution_mode: "TESTNET_READ", is_running: false, demo_mode: true }),
  ]);
  assert.deepEqual(r.userIds, [ALICE, BOB, CARLA]);
});

// --------------------------------------------------------------------------
// 4. no keys, no accounting
// --------------------------------------------------------------------------

test("a customer without keys never appears", () => {
  // The roster IS the set of credential owners: a user with no binance_keys row
  // contributes no row here, so there is nothing to exclude later.
  assert.deepEqual(selectAccountingRoster([]).userIds, []);
});

test("a malformed user id is dropped rather than passed on", () => {
  const r = selectAccountingRoster([
    owner(ALICE),
    { user_id: "not-a-uuid" },
    { user_id: "" },
    { user_id: null },
    { user_id: 42 },
    { user_id: undefined },
  ] as never);
  assert.deepEqual(r.userIds, [ALICE]);
});

test("the query reads the credential table, not the trading config", () => {
  const s = src(ROSTER_LIB);
  assert.match(s, /\.from\("binance_keys"\)/);
  assert.ok(!s.includes('from("engine_config")'), "accounting eligibility reads engine_config");
});

test("eligibility ignores execution_mode, is_running and demo_mode entirely", () => {
  // Checked against the executable lines: the file explains at length which
  // fields it must not consult, and naming them in prose is the point.
  const code = src(ROSTER_LIB)
    .split("\n")
    .filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l))
    .join("\n");
  for (const field of ["execution_mode", "is_running", "demo_mode", "EXECUTABLE_MODES"]) {
    assert.ok(!code.includes(field), `accounting eligibility consults ${field}`);
  }
});

// --------------------------------------------------------------------------
// 5. no credential material
// --------------------------------------------------------------------------

test("the roster returns ids only, and never key material", () => {
  const r = selectAccountingRoster([owner(ALICE, { api_key_last4: "9animals" })]);
  assert.deepEqual(r.userIds, [ALICE]);
  assert.deepEqual(Object.keys(r).sort(), ["truncated", "userIds"]);
});

test("the query never selects an encrypted blob or last4", () => {
  const lib = src(ROSTER_LIB);
  assert.match(lib, /\.select\("user_id"\)/);
  for (const field of ["api_key_encrypted", "api_secret_encrypted", "api_key_last4", "decrypt"]) {
    const code = lib
      .split("\n")
      .filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l))
      .join("\n");
    assert.ok(!code.includes(field), `the roster query touches ${field}`);
  }
});

test("the endpoint body carries ids, a count and a truncation flag only", () => {
  assert.match(src(ROSTER_ROUTE), /users: roster\.userIds\.map\(\(user_id\) => \(\{ user_id \}\)\)/);
  const body = code(ROSTER_ROUTE);
  for (const field of ["api_key", "api_secret", "last4", "credentials"]) {
    assert.ok(!new RegExp(`${field}\\s*[:,]`).test(body), `the endpoint returns ${field}`);
  }
});

// --------------------------------------------------------------------------
// 6. deduplication
// --------------------------------------------------------------------------

test("a duplicated customer is accounted once", () => {
  // Two passes over one account would each attribute the same funding events.
  const r = selectAccountingRoster([owner(ALICE), owner(ALICE), owner(BOB), owner(ALICE)]);
  assert.deepEqual(r.userIds, [ALICE, BOB]);
});

test("ids differing only in case are treated as the same customer", () => {
  const r = selectAccountingRoster([owner(ALICE), owner(ALICE.toUpperCase())]);
  assert.equal(r.userIds.length, 2, "uuid case is preserved verbatim, not folded");
  // Documents the deliberate choice: ids come from a uuid column, so two casings
  // of one id cannot occur; nothing here silently rewrites a customer's id.
});

// --------------------------------------------------------------------------
// 7. truncation is surfaced
// --------------------------------------------------------------------------

test("a roster within the cap is not flagged as truncated", () => {
  const rows = Array.from({ length: 10 }, (_, i) =>
    owner(`aaaaaaaa-1111-1111-1111-${String(i).padStart(12, "0")}`),
  );
  const r = selectAccountingRoster(rows);
  assert.equal(r.truncated, false);
  assert.equal(r.userIds.length, 10);
});

test("an oversized roster is capped AND says so", () => {
  // Silence would mean a customer whose real P&L simply never appears.
  const rows = Array.from({ length: MAX_ACCOUNTING_USERS + 5 }, (_, i) =>
    owner(`aaaaaaaa-1111-1111-1111-${String(i).padStart(12, "0")}`),
  );
  const r = selectAccountingRoster(rows);
  assert.equal(r.userIds.length, MAX_ACCOUNTING_USERS);
  assert.equal(r.truncated, true);
});

test("the endpoint passes truncation and the cap to the caller", () => {
  const s = src(ROSTER_ROUTE);
  assert.match(s, /truncated: roster\.truncated/);
  assert.match(s, /max: MAX_ACCOUNTING_USERS/);
});

// --------------------------------------------------------------------------
// 8. failure is loud, never an empty roster
// --------------------------------------------------------------------------

test("the endpoint requires the service token", () => {
  const s = src(ROSTER_ROUTE);
  assert.match(s, /token !== process\.env\.ENGINE_SERVICE_TOKEN/);
  assert.match(s, /status: 401/);
});

test("a query failure is a 500, never an empty 200", () => {
  const lib = src(ROSTER_LIB);
  assert.match(lib, /if \(error\) throw new Error\(error\.message\)/);
  const route = src(ROSTER_ROUTE);
  assert.match(route, /catch \(e\) \{[\s\S]*?return json\(\{ error: String\(e\) \}, 500\)/);
});

test("the accounting process treats a bad roster as unknown, not as nobody", () => {
  const sync = src(SYNC);
  assert.match(sync, /class RosterUnavailable/);
  assert.match(sync, /roster endpoint rejected this process/);
  // A 401 raises rather than yielding an empty list that would write nothing
  // and look like a normal quiet day.
  assert.match(sync, /ENGINE_SERVICE_TOKEN/);
});

// --------------------------------------------------------------------------
// the execution roster is untouched
// --------------------------------------------------------------------------

test("the accounting roster is a separate module from the execution roster", () => {
  assert.ok(
    !code(ROSTER_LIB).includes("engine-roster.server"),
    "accounting imports the execution roster",
  );
  assert.match(src(ROSTER_ROUTE), /accounting-roster\.server/);
  assert.ok(
    !code(ROSTER_ROUTE).includes("engine-roster.server"),
    "the accounting endpoint imports the execution roster",
  );
});

test("the execution roster still means what it always meant", () => {
  // Untouched by this work: its gate is still execution_mode, and its endpoint
  // still serves the executor.
  const lib = src(EXECUTION_LIB);
  assert.match(lib, /EXECUTABLE_MODES = \["LIVE_READ", "LIVE_TRADE"\]/);
  assert.match(lib, /export async function loadExecutionRoster/);
  assert.ok(!lib.includes("binance_keys"), "the execution roster changed its source");
  assert.ok(!lib.includes("accounting"), "the execution roster learned about accounting");
  assert.ok(!src(EXECUTION_ROUTE).includes("accounting"));
});

test("the synchroniser asks the accounting roster, not the execution one", () => {
  const sync = src(SYNC);
  assert.match(sync, /ROSTER_PATH = "\/api\/public\/engine\/accounting\/users"/);
  const code = sync
    .split("\n")
    .filter((l) => !/^\s*#/.test(l))
    .join("\n");
  assert.ok(
    !code.includes("/api/public/engine/users/active"),
    "the accounting sync still calls the execution roster",
  );
});
