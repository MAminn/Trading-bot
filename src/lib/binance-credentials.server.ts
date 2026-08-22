// Server-only lookup that turns a user's stored Binance key row into usable
// plaintext credentials. Trusted context only — never import this from a
// component, a loader, or any route that a browser can reach.
//
// This is the ONLY place outside crypto.server.ts that produces plaintext key
// material, and it is deliberately separate from the route that serves it so
// the decision rules can be tested without standing up a request.
//
// Two encryption mechanisms have existed for this table. The live one is
// AES-256-GCM written by saveBinanceKeys() via crypto.server.ts. The other is
// the legacy pgcrypto pair (save_binance_keys / decrypt_binance_keys_for),
// which used a Postgres GUC passphrase and produced blobs this code cannot
// read. Rows in that format are reported as undecryptable rather than guessed
// at: a key we cannot read is a key we must not trade with.
import { decryptBuffer } from "./crypto.server.ts";

export type CredentialLookup =
  | { status: "ok"; apiKey: string; apiSecret: string; last4: string }
  | { status: "missing" }
  | { status: "undecryptable" };

// PostgREST renders bytea as a hex string prefixed with a literal backslash-x.
// Anything else is a shape this code was not written for, and is refused
// rather than coerced.
export function parseByteaHex(value: unknown): Buffer | null {
  if (typeof value !== "string") return null;
  if (!value.startsWith("\\x")) return null;
  const hex = value.slice(2);
  if (hex.length === 0 || hex.length % 2 !== 0) return null;
  if (!/^[0-9a-fA-F]+$/.test(hex)) return null;
  return Buffer.from(hex, "hex");
}

export type KeyRow = {
  api_key_encrypted?: unknown;
  api_secret_encrypted?: unknown;
  api_key_last4?: unknown;
} | null;

/**
 * Decrypt one stored key row. Pure — no I/O, so the caller owns the query and
 * this stays testable without a database.
 *
 * Never throws on bad input and never includes key material or decrypt
 * internals in its result: the three statuses are the entire vocabulary, so a
 * caller cannot accidentally surface a cipher error containing a fragment of
 * the plaintext.
 */
export function decryptKeyRow(row: KeyRow): CredentialLookup {
  if (!row) return { status: "missing" };
  const keyBuf = parseByteaHex(row.api_key_encrypted);
  const secretBuf = parseByteaHex(row.api_secret_encrypted);
  if (!keyBuf || !secretBuf) return { status: "undecryptable" };
  let apiKey: string;
  let apiSecret: string;
  try {
    apiKey = decryptBuffer(keyBuf);
    apiSecret = decryptBuffer(secretBuf);
  } catch {
    // Swallowed deliberately. A GCM failure means a wrong key, a tampered blob
    // or a legacy pgcrypto row; none of those are distinguishable to a caller
    // that must fail closed anyway, and the exception text is not worth
    // routing towards a response.
    return { status: "undecryptable" };
  }
  if (!apiKey || !apiSecret) return { status: "undecryptable" };
  const last4 =
    typeof row.api_key_last4 === "string" && row.api_key_last4
      ? row.api_key_last4
      : apiKey.slice(-4);
  return { status: "ok", apiKey, apiSecret, last4 };
}

/** Fetch and decrypt the live Binance credentials for one user. */
export async function loadUserBinanceCredentials(userId: string): Promise<CredentialLookup> {
  const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
  const { data, error } = await supabaseAdmin
    .from("binance_keys")
    .select("api_key_encrypted,api_secret_encrypted,api_key_last4")
    .eq("user_id", userId)
    .maybeSingle();
  if (error || !data) return { status: "missing" };
  return decryptKeyRow(data as KeyRow);
}

/**
 * The bearer token the executor-only credentials endpoint accepts, or null when
 * it is not safely configured.
 *
 * Lives here rather than in the route so it can be tested: "must differ from
 * ENGINE_SERVICE_TOKEN" is the entire reason a second token exists, and a rule
 * that only deployment discipline enforces is not a rule.
 */
export function resolveCredentialsToken(env: Record<string, string | undefined>): string | null {
  const token = (env.ENGINE_CREDENTIALS_TOKEN ?? "").trim();
  if (!token) return null;
  // Reusing the service token would silently re-widen access to every holder of
  // it — the ingest, config, signal and telemetry routes all accept that one.
  if (token === (env.ENGINE_SERVICE_TOKEN ?? "").trim()) return null;
  return token;
}
