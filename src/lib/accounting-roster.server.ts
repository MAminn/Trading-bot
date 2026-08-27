// Who has real Binance money to account for. Server-only.
//
// Deliberately NOT engine-roster.server.ts, and deliberately not derived from
// it. That file answers a different question:
//
//   EXECUTION ROSTER    who the executor should build a trading session for.
//                       Gated on `execution_mode IN (LIVE_READ, LIVE_TRADE)`,
//                       which is exactly right for deciding who to trade for.
//
//   ACCOUNTING ROSTER   (this file) whose completed Binance trades still need
//                       their commission and net P&L synchronised.
//
// Using the execution roster for accounting would lose a customer's real
// financial history the moment they stopped trading. A trade that has already
// happened on Binance charged real commission and moved real money; pressing
// Stop, switching execution_mode to OFF, or turning off live execution changes
// nothing about that trade, and must not make it disappear from the customer's
// own P&L. So eligibility here is neither `is_running` nor `execution_mode`:
//
//   a customer is accountable if they have Binance credentials on file.
//
// That is the only condition under which there is an account to read. A user
// who never connected a wallet — including a demo-only user — has no Binance
// history, so they are absent rather than skipped later.
//
// NO KEY MATERIAL. The query selects `user_id` and nothing else: not the
// encrypted blobs, not last4, not a decrypt attempt. Presence of a row is the
// entire signal. Deciding whether those keys actually DECRYPT stays with the
// credentials endpoint, which the accounting process already calls per user and
// which fails closed on its own — pulling that decision forward would mean
// decrypting every customer's secret just to build a list of ids.

// Accounting runs sequentially, holds no persistent exchange connection, and is
// read-only, so it can carry a larger roster than the executor's session cap.
// It is still bounded, and a truncated roster is reported rather than silently
// applied: silence would mean a customer whose real P&L simply never appears.
export const MAX_ACCOUNTING_USERS = 1000;

export type KeyOwnerRow = { user_id: unknown };

function validUserId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return /^[0-9a-f-]{36}$/i.test(trimmed) ? trimmed : null;
}

/**
 * Turn credential-owner rows into the accounting roster: validated,
 * deduplicated, capped.
 *
 * Pure, so the eligibility rule can be tested exhaustively without a database.
 */
export function selectAccountingRoster(rows: KeyOwnerRow[]): {
  userIds: string[];
  truncated: boolean;
} {
  const seen = new Set<string>();
  for (const row of rows) {
    const id = validUserId(row.user_id);
    // A malformed id cannot be accounted against and must not be passed on:
    // the synchroniser would ask the credentials endpoint for a user that
    // cannot be looked up.
    if (id === null) continue;
    seen.add(id);
  }
  const all = [...seen];
  return {
    userIds: all.slice(0, MAX_ACCOUNTING_USERS),
    truncated: all.length > MAX_ACCOUNTING_USERS,
  };
}

/**
 * Fetch the accounting roster.
 *
 * Throws on a query failure rather than returning an empty list. An empty
 * roster and a broken database look identical to the caller otherwise, and the
 * accounting process would read that as "no customers today" and quietly write
 * nothing at all.
 */
export async function loadAccountingRoster(): Promise<{
  userIds: string[];
  truncated: boolean;
}> {
  const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
  // user_id only. Never api_key_encrypted, api_secret_encrypted or last4.
  const { data, error } = await supabaseAdmin.from("binance_keys").select("user_id");
  if (error) throw new Error(error.message);
  return selectAccountingRoster((data ?? []) as KeyOwnerRow[]);
}
