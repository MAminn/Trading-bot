// Secret containment: where key material may go, and everywhere it may not.
//
//   node --test src/lib/binance-credentials.test.ts
//
// The production blocker this guards against had two halves. The executor half
// (it signed with the server's .env keys instead of the client's) is covered by
// executor/test_user_credentials.py. This is the app half: exactly one endpoint
// may return a decrypted key, it must be behind its own token, and every other
// route that touches the binance_keys table must stay metadata-only.
//
// The route files import @tanstack/react-router and cannot be imported here, so
// the route-shape assertions read the source as text — the same approach, and
// the same reasoning, as executor-status-contract.test.ts.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Set before importing crypto.server: getKey() reads it lazily per call, but a
// missing value throws, and every round-trip below depends on it.
process.env.BINANCE_KEY_ENCRYPTION_SECRET = "test-only-secret-not-a-real-one";

const { encryptString } = await import("./crypto.server.ts");
const { decryptKeyRow, parseByteaHex, resolveCredentialsToken } =
  await import("./binance-credentials.server.ts");

const CONFIG_ROUTE = "src/routes/api/public/engine/config.ts";
const CREDENTIALS_ROUTE = "src/routes/api/public/engine/credentials.ts";
const STATUS_ROUTE = "src/routes/api/public/engine/ingest.executor_status.ts";

const API_KEY = "PUBLICKEYaaaaaaaaaaaaaaaaaaaaaaaa1234";
const API_SECRET = "SECRETbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

function bytea(plain: string): string {
  return `\\x${encryptString(plain).toString("hex")}`;
}

// --------------------------------------------------------------------------
// The public config endpoint must never carry key material
// --------------------------------------------------------------------------

test("the public config endpoint selects no key columns", () => {
  const src = readFileSync(CONFIG_ROUTE, "utf8");
  // The endpoint whitelists columns rather than SELECT *, so this is checkable
  // as an absence: a future column cannot leak by merely existing.
  for (const forbidden of ["api_key_encrypted", "api_secret_encrypted"]) {
    assert.equal(src.includes(forbidden), false, `${CONFIG_ROUTE} must not reference ${forbidden}`);
  }
});

test("the public config endpoint never decrypts", () => {
  const src = readFileSync(CONFIG_ROUTE, "utf8");
  for (const forbidden of [
    "decryptBuffer",
    "decryptKeyRow",
    "loadUserBinanceCredentials",
    "crypto.server",
  ]) {
    assert.equal(src.includes(forbidden), false, `${CONFIG_ROUTE} must not reference ${forbidden}`);
  }
});

test("the public config endpoint exposes last4 and a presence flag only", () => {
  const src = readFileSync(CONFIG_ROUTE, "utf8");
  // The only binance_keys column it may read.
  const selected = src.match(/\.select\("([^"]*)"\)/g) ?? [];
  const keySelect = selected.find((s) => s.includes("api_key_last4"));
  assert.ok(keySelect, "expected the last4-only select to still be present");
  assert.equal(keySelect.includes("encrypted"), false, "the key select must stay metadata-only");
  assert.ok(src.includes("keys_present"), "presence flag should still be served");
});

test("the executor status ingest schema accepts no key fields", () => {
  const src = readFileSync(STATUS_ROUTE, "utf8");
  // Telemetry is a sink the executor writes to every cycle. A key field here
  // would persist secrets into a table the dashboard reads.
  for (const forbidden of ["api_key:", "api_secret", "encrypted"]) {
    assert.equal(src.includes(forbidden), false, `${STATUS_ROUTE} must not accept ${forbidden}`);
  }
  // last4 is not a secret, but it is also not telemetry — the Engine page reads
  // it from the user's own row instead, so it has no business on this schema.
  assert.equal(src.includes("api_key_last4"), false);
});

// --------------------------------------------------------------------------
// The credentials endpoint is the single, deliberately narrow exception
// --------------------------------------------------------------------------

test("only one route in the app returns decrypted key material", () => {
  const routes = [CONFIG_ROUTE, CREDENTIALS_ROUTE, STATUS_ROUTE];
  const decrypting = routes.filter((path) =>
    readFileSync(path, "utf8").includes("loadUserBinanceCredentials"),
  );
  assert.deepEqual(decrypting, [CREDENTIALS_ROUTE]);
});

test("the credentials endpoint sends no CORS headers", () => {
  const src = readFileSync(CREDENTIALS_ROUTE, "utf8");
  // Every sibling route answers `*` because a browser legitimately calls it.
  // Nothing in a browser may call this one, so the absence of these headers is
  // itself a control: an accidental fetch() from the app is blocked by the
  // browser before the request is ever made.
  assert.equal(src.includes("Access-Control-Allow-Origin"), false);
  assert.equal(src.includes("Access-Control-Allow-Headers"), false);
});

test("the credentials endpoint forbids caching of its response", () => {
  const src = readFileSync(CREDENTIALS_ROUTE, "utf8");
  assert.ok(src.includes("no-store"), "key material must not be cached");
});

test("the credentials endpoint exposes no write or browser-reachable method", () => {
  const src = readFileSync(CREDENTIALS_ROUTE, "utf8");
  assert.ok(src.includes("GET:"), "expected a GET handler");
  for (const method of ["POST:", "PUT:", "PATCH:", "DELETE:", "OPTIONS:"]) {
    assert.equal(src.includes(method), false, `${CREDENTIALS_ROUTE} must not define ${method}`);
  }
});

test("the credentials endpoint reports a missing row with the executor's reason", () => {
  const src = readFileSync(CREDENTIALS_ROUTE, "utf8");
  // The executor turns this exact string into its blocked_reason, and
  // executor/user_credentials.py asserts the same constant from its side.
  assert.ok(src.includes("missing_user_binance_keys"));
  assert.ok(src.includes("credentials_undecryptable"));
});

// --------------------------------------------------------------------------
// Token separation
// --------------------------------------------------------------------------

test("the credentials token must be set", () => {
  assert.equal(resolveCredentialsToken({}), null);
  assert.equal(resolveCredentialsToken({ ENGINE_CREDENTIALS_TOKEN: "   " }), null);
});

test("the credentials token must differ from the service token", () => {
  // The whole reason a second token exists: the service token is accepted by
  // the ingest, config and signal routes, and key material must not sit behind
  // a token that many endpoints already take.
  assert.equal(
    resolveCredentialsToken({
      ENGINE_CREDENTIALS_TOKEN: "shared",
      ENGINE_SERVICE_TOKEN: "shared",
    }),
    null,
  );
  assert.equal(
    resolveCredentialsToken({
      ENGINE_CREDENTIALS_TOKEN: " shared ",
      ENGINE_SERVICE_TOKEN: "shared",
    }),
    null,
    "whitespace must not be a way around the separation",
  );
});

test("a distinct credentials token is accepted", () => {
  assert.equal(
    resolveCredentialsToken({
      ENGINE_CREDENTIALS_TOKEN: "credentials-token",
      ENGINE_SERVICE_TOKEN: "service-token",
    }),
    "credentials-token",
  );
});

// --------------------------------------------------------------------------
// Decryption, in the one place it is allowed to happen
// --------------------------------------------------------------------------

test("a row written by saveBinanceKeys round-trips", () => {
  const result = decryptKeyRow({
    api_key_encrypted: bytea(API_KEY),
    api_secret_encrypted: bytea(API_SECRET),
    api_key_last4: "1234",
  });
  assert.equal(result.status, "ok");
  assert.equal(result.status === "ok" && result.apiKey, API_KEY);
  assert.equal(result.status === "ok" && result.apiSecret, API_SECRET);
  assert.equal(result.status === "ok" && result.last4, "1234");
});

test("last4 falls back to the key when the column is empty", () => {
  const result = decryptKeyRow({
    api_key_encrypted: bytea(API_KEY),
    api_secret_encrypted: bytea(API_SECRET),
    api_key_last4: "",
  });
  assert.equal(result.status === "ok" && result.last4, API_KEY.slice(-4));
});

test("no row is 'missing', never an error", () => {
  // A user who has not connected is a state the executor reports, not a fault.
  assert.deepEqual(decryptKeyRow(null), { status: "missing" });
});

test("a legacy pgcrypto row is undecryptable, not silently wrong", () => {
  // save_binance_keys() (the old SQL function) wrote pgp_sym_encrypt blobs with
  // a Postgres GUC passphrase. AES-256-GCM cannot read them. Reporting that
  // distinctly is what stops an operator hunting for a user who "has no keys"
  // when in fact the row is there and unreadable.
  const pgpLike = `\\x${"c30d04070302".padEnd(120, "ab")}`;
  assert.deepEqual(
    decryptKeyRow({
      api_key_encrypted: pgpLike,
      api_secret_encrypted: pgpLike,
      api_key_last4: "1234",
    }),
    { status: "undecryptable" },
  );
});

test("a tampered ciphertext is refused rather than returned", () => {
  const good = bytea(API_KEY);
  // Flip the final byte: GCM authenticates, so this must fail the tag check.
  const tampered = good.slice(0, -2) + (good.slice(-2) === "00" ? "01" : "00");
  assert.deepEqual(
    decryptKeyRow({
      api_key_encrypted: tampered,
      api_secret_encrypted: bytea(API_SECRET),
      api_key_last4: "1234",
    }),
    { status: "undecryptable" },
  );
});

test("a decrypt failure never carries key material in its result", () => {
  const result = decryptKeyRow({
    api_key_encrypted: "\\xdeadbeef",
    api_secret_encrypted: "\\xdeadbeef",
    api_key_last4: "1234",
  });
  // The three statuses are the entire vocabulary, so no cipher error text — or
  // fragment of a plaintext — can be routed towards a response.
  assert.deepEqual(Object.keys(result), ["status"]);
});

test("malformed bytea shapes are refused, not coerced", () => {
  for (const bad of [
    undefined,
    null,
    42,
    "",
    "deadbeef", // no \x prefix
    "\\x", // empty
    "\\xabc", // odd length
    "\\xzz11", // not hex
  ]) {
    assert.equal(parseByteaHex(bad), null, `expected null for ${String(bad)}`);
  }
  assert.ok(Buffer.isBuffer(parseByteaHex("\\xdeadbeef")));
});

// --------------------------------------------------------------------------
// Nothing browser-reachable may import the decrypting module
// --------------------------------------------------------------------------

test("the server functions return only metadata to the browser", () => {
  const src = readFileSync("src/lib/binance.functions.ts", "utf8");
  // saveBinanceKeys encrypts and returns last4; getBinanceKeyInfo goes through
  // an RPC that selects last4 + metadata. Neither may gain a decrypt.
  assert.equal(src.includes("decryptBuffer"), false);
  assert.equal(src.includes("binance-credentials.server"), false);
  assert.ok(src.includes("get_my_binance_key_info"));
});

test("no .tsx component imports the credential loader", async () => {
  const { globSync } = await import("node:fs");
  const files = globSync("src/**/*.tsx");
  assert.ok(files.length > 0, "expected to find components to check");
  for (const file of files) {
    const src = readFileSync(file, "utf8");
    assert.equal(
      src.includes("binance-credentials.server"),
      false,
      `${file} must not import the credential loader`,
    );
    assert.equal(
      src.includes("crypto.server"),
      false,
      `${file} must not import the decryption helpers`,
    );
  }
});
