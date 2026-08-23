// The executor is real, and the UI has to say so — accurately.
//
//   node --test src/lib/executor-link.test.ts
//
// The bug this locks down: Configure → Live execution carried a banner telling
// the client the executor did not read their settings and still followed its own
// environment on the VPS. That was true of the single-tenant executor. It
// stopped being true with multi-tenant onboarding — the executor polls the
// roster, reads each user's config every cycle and fetches their own Binance
// credentials — and a paying client reading it concludes the product is a
// mock-up.
//
// Two things are tested, and they pull in opposite directions on purpose:
//
//   1. The stale sentences cannot come back. A plain text scan of all of src/.
//   2. The replacement does not overshoot. "The executor reads your settings"
//      is now true for everyone; "the executor is linked to your wallet" is
//      true only once a signed Binance read has actually come back, and no
//      amount of connected keys or ready telemetry may be promoted into it.

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { extname, join } from "node:path";
import { test } from "node:test";

const {
  EXECUTOR_CONNECTED_TITLE,
  EXECUTOR_LINK_LABEL,
  EXECUTOR_LIVE_ORDER_REQUIREMENTS,
  EXECUTOR_READS_SETTINGS,
  resolveExecutorLink,
} = await import("./executor-link.ts");

const SRC = "src";
const CONFIGURE = "src/routes/app.configure.tsx";
const ENGINE = "src/routes/app.engine.tsx";
const DASHBOARD = "src/routes/app.dashboard.tsx";

/** A heartbeat recent enough to count as live. */
const NOW = () => new Date().toISOString();

function row(over: Record<string, unknown> = {}) {
  return {
    wallet_balance_usd: null,
    available_balance_usd: null,
    keys_present: null,
    last_heartbeat: NOW(),
    ...over,
  } as never;
}

/** Every source file under src/, so a stale sentence cannot survive by being
 *  moved to a component, a constant file, or a route this test did not name.
 *  Paths are normalised to forward slashes so the assertions read the same on
 *  Windows as on the deploy host. */
function sourceFiles(dir: string = SRC): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name).split("\\").join("/");
    if (entry.isDirectory()) out.push(...sourceFiles(path));
    else if ([".ts", ".tsx", ".js", ".jsx", ".md"].includes(extname(entry.name))) out.push(path);
  }
  return out;
}

// --------------------------------------------------------------------------
// The stale claim cannot come back
// --------------------------------------------------------------------------

// The exact sentences that told the client their bot was not wired up. They are
// assembled from fragments rather than written out, because this file is itself
// inside the scanned tree — a scan that matched its own source would be
// permanently red and would have to be weakened to pass.
const FORBIDDEN = [
  ["Not yet connected to the ", "executor"].join(""),
  ["executor does ", "not read them yet"].join(""),
];

test("the stale 'not connected to the executor' copy is absent from all of src", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles()) {
    const src = readFileSync(file, "utf8");
    for (const phrase of FORBIDDEN) {
      if (src.includes(phrase)) offenders.push(`${file}: "${phrase}"`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    `stale executor copy is back in the product:\n${offenders.join("\n")}`,
  );
});

test("the scan actually reaches the pages the copy lived on", () => {
  // A guard on the guard: if sourceFiles() ever stopped walking routes/, the
  // test above would pass by reading nothing at all.
  const files = sourceFiles();
  for (const page of [CONFIGURE, ENGINE, DASHBOARD]) {
    assert.ok(files.includes(page), `${page} should be inside the scanned set`);
  }
  assert.ok(files.length > 20, "the scan should be walking the tree, not one directory");
});

// --------------------------------------------------------------------------
// The replacement is on the page
// --------------------------------------------------------------------------

test("the production copy says what it is required to say", () => {
  assert.equal(EXECUTOR_CONNECTED_TITLE, "Connected to multi-tenant executor");
  assert.equal(
    EXECUTOR_READS_SETTINGS,
    "The executor reads this user's live settings from the database each cycle.",
  );
  // Naming a subset of the gates would imply the rest are already satisfied.
  for (const gate of [
    "connected Binance keys",
    "Start enabled",
    "LIVE_TRADE selected",
    "auto-execute enabled",
    "host LIVE_TRADE ceiling",
    "ACK",
    "live order cap",
  ]) {
    assert.ok(
      EXECUTOR_LIVE_ORDER_REQUIREMENTS.includes(gate),
      `the requirements sentence should still name "${gate}"`,
    );
  }
});

test("Configure states the real relationship and the full gate list", () => {
  const src = readFileSync(CONFIGURE, "utf8");
  assert.ok(
    src.includes("EXECUTOR_CONNECTED_TITLE"),
    "Configure should render the connected title",
  );
  assert.ok(
    src.includes("EXECUTOR_READS_SETTINGS"),
    "Configure should say what the executor reads",
  );
  assert.ok(
    src.includes("EXECUTOR_LIVE_ORDER_REQUIREMENTS"),
    "Configure should list every gate a live order still has to pass",
  );
});

test("Configure, Engine and Dashboard all speak through the shared copy", () => {
  // Consistency by construction: none of the three may hand-write a state
  // sentence, because all three read the same exported strings.
  for (const page of [CONFIGURE, ENGINE]) {
    const src = readFileSync(page, "utf8");
    assert.ok(src.includes("ExecutorLinkRow"), `${page} should render the shared link row`);
    assert.ok(src.includes("resolveExecutorLink"), `${page} should resolve the link centrally`);
  }
  const dash = readFileSync(DASHBOARD, "utf8");
  assert.ok(dash.includes("EXECUTOR_LINK_LABEL"), "the dashboard should reuse the shared labels");
  assert.ok(
    dash.includes("resolveExecutorLink"),
    "the dashboard should resolve the link centrally",
  );
});

test("no page reads a balance straight off the telemetry row", () => {
  // Requirement 6, as source: a figure may only reach a client through
  // resolveWalletDisplay, which refuses to hand one back until a signed read
  // exists. The Engine card used to render row.wallet_balance_usd directly,
  // which kept showing the last known balance after a client withdrew keys.
  for (const page of [ENGINE, DASHBOARD]) {
    const src = readFileSync(page, "utf8");
    assert.ok(src.includes("resolveWalletDisplay"), `${page} should resolve balances centrally`);
    for (const raw of ["fmtUSD(row.wallet_balance_usd)", "fmtUSD(row.available_balance_usd)"]) {
      assert.equal(src.includes(raw), false, `${page} renders ${raw} without the wallet rule`);
    }
  }
});

test("no page hard-codes a link sentence instead of importing it", () => {
  // Each label may appear in exactly one place: the module that defines it.
  // Anywhere else is a second copy, and a second copy is a future disagreement.
  const labels = Object.values(EXECUTOR_LINK_LABEL) as string[];
  const pages = sourceFiles().filter((f) => !f.startsWith("src/lib/executor-link"));
  for (const file of pages) {
    const src = readFileSync(file, "utf8");
    for (const label of labels) {
      assert.equal(
        src.includes(label),
        false,
        `${file} hard-codes "${label}" instead of importing EXECUTOR_LINK_LABEL`,
      );
    }
  }
});

// --------------------------------------------------------------------------
// The three states, and the line the wallet claim may not cross
// --------------------------------------------------------------------------

test("no keys connected asks the client to connect Binance first", () => {
  assert.equal(resolveExecutorLink(null), "not_connected");
  assert.equal(resolveExecutorLink(null, { keysConnected: false }), "not_connected");
  assert.equal(EXECUTOR_LINK_LABEL.not_connected, "Connect Binance first");
});

test("keys connected but nothing read yet is waiting, not linked", () => {
  // Seconds after the client saves their keys the executor has not completed a
  // cycle. Claiming a wallet link here is a promise the data cannot back.
  assert.equal(resolveExecutorLink(null, { keysConnected: true }), "awaiting_read");
  assert.equal(EXECUTOR_LINK_LABEL.awaiting_read, "Waiting for first executor read");
});

test("keys_present=true without a balance is still only waiting", () => {
  // keys_present is the executor saying it HAS the keys, not that it has used
  // them. Promoting that to "linked to your wallet" is the same borrowed
  // confidence that produced the phantom $10,000 equity tile.
  assert.equal(resolveExecutorLink(row({ keys_present: true })), "awaiting_read");
  assert.equal(
    resolveExecutorLink(row({ keys_present: true, wallet_balance_usd: "1234" })),
    "awaiting_read",
  );
});

test("a signed read links the executor to the client's wallet", () => {
  assert.equal(
    resolveExecutorLink(row({ keys_present: true, wallet_balance_usd: 512.34 })),
    "linked",
  );
  assert.equal(EXECUTOR_LINK_LABEL.linked, "Executor linked to your Binance wallet");
});

test("a genuine zero balance is a read, and counts as linked", () => {
  assert.equal(resolveExecutorLink(row({ keys_present: true, wallet_balance_usd: 0 })), "linked");
});

test("a withdrawn key drops the link even with a balance left on the row", () => {
  assert.equal(
    resolveExecutorLink(row({ keys_present: false, wallet_balance_usd: 4200 })),
    "not_connected",
  );
});

test("a stale heartbeat does not downgrade the link", () => {
  // The reading is old, which the wallet tile flags separately. The account is
  // still linked, and saying otherwise would be its own false statement.
  assert.equal(
    resolveExecutorLink(
      row({
        keys_present: true,
        wallet_balance_usd: 250,
        last_heartbeat: "2020-01-01T00:00:00.000Z",
      }),
    ),
    "linked",
  );
});
