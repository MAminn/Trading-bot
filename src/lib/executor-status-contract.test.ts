// Guards the executor -> app telemetry contract.
//
//   node --test src/lib/executor-status-contract.test.ts
//
// Why this exists: the ingest route validates with a Zod object, and z.object()
// STRIPS unknown keys rather than rejecting them. A field the executor sends
// but the schema omits is therefore discarded silently — no error, no log, the
// column just stays null. That is exactly what happened to the six Phase 3
// live-control fields in production.
//
// The route imports @tanstack/react-router, so it cannot be imported here.
// This reads the route source instead and asserts every field the executor
// sends appears in the schema. Crude, but it catches precisely the class of
// drift that produced the incident, and it fails loudly when someone adds a
// field to the executor payload without adding it here.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const ROUTE = "src/routes/api/public/engine/ingest.executor_status.ts";
const SNAPSHOT = "executor/executor_status.py";

// Every key executor_status.build_snapshot() puts on the wire.
const EXPECTED_FIELDS = [
  "effective_mode",
  "env_mode_ceiling",
  "db_execution_mode",
  "auto_execute_enabled",
  "orders_enabled",
  "blocked_reason",
  "wallet_balance_usd",
  "available_balance_usd",
  "position_amt",
  "position_side",
  "entry_price",
  "position_leverage",
  "margin_type",
  "reconcile_match",
  "reconcile_expected",
  "reconcile_actual",
  "last_reconcile_at",
  "keys_present",
  "permission_status",
  "message",
];

test("the ingest schema accepts every field the executor sends", () => {
  const route = readFileSync(ROUTE, "utf8");
  const schema = route.slice(
    route.indexOf("const Body = z.object({"),
    route.indexOf("export const EXECUTOR_STATUS_FIELDS"),
  );
  assert.ok(schema.length > 0, "could not locate the Body schema in the route");

  const missing = EXPECTED_FIELDS.filter((f) => !schema.includes(`${f}:`));
  assert.deepEqual(
    missing,
    [],
    `these fields are sent by the executor but absent from the ingest schema, ` +
      `so z.object() will strip them and their columns will stay null: ${missing.join(", ")}`,
  );
});

test("the executor's snapshot builder emits exactly the expected field set", () => {
  // The other direction: a field added to the schema but never sent, or sent
  // under a different name, is just as wrong.
  const py = readFileSync(SNAPSHOT, "utf8");
  const snapshotBlock = py.slice(
    py.indexOf("    snapshot = {"),
    py.indexOf("    if reconcile is not None:"),
  );
  assert.ok(snapshotBlock.length > 0, "could not locate the snapshot dict");

  const emitted = [...snapshotBlock.matchAll(/^\s{8}"([a-z_]+)":/gm)].map((m) => m[1]);
  const unexpected = emitted.filter((f) => !EXPECTED_FIELDS.includes(f));
  assert.deepEqual(
    unexpected,
    [],
    `the executor sends fields this contract does not know about: ${unexpected.join(", ")}`,
  );
});
