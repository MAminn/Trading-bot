// What the UI is allowed to present as the client's money.
//
// Deliberately dependency-free: no Supabase client, no React, no imports at all.
// This is the rule that decides whether a currency figure gets shown to a client
// as their balance, so it is kept where it can be tested directly and read in
// one sitting.
//
// The bug it exists to prevent: the dashboard rendered
// `capital_usd + strategy P&L` under the label "Equity". `capital_usd` defaults
// to 10,000, so a brand-new user who had connected nothing was greeted with
// "Equity $10,000" — a configuration default presented as funds. A client
// reasonably reads that as the platform holding ten thousand dollars for them.
//
// The rule: a figure may be called the client's balance ONLY when it came from
// a signed read of their own Binance account. Everything else is a model
// quantity and must be labelled as one.

/** Matches engine.ts's HEARTBEAT_FRESH_MS. A reading older than this describes
 *  the past, and is flagged rather than presented as current. */
export const WALLET_FRESH_MS = 3 * 60_000;

export type WalletDisplay =
  | {
      state: "connected";
      walletUsd: number;
      availableUsd: number | null;
      /** The heartbeat behind this reading is old. */
      stale: boolean;
    }
  /** Keys are connected but no balance has come back yet. Shows a dash, never a
   *  zero: "not read yet" is a different claim from "your balance is zero". */
  | { state: "awaiting_read" }
  /** No Binance account is linked. */
  | { state: "not_connected" };

export type WalletSource =
  | {
      wallet_balance_usd?: unknown;
      available_balance_usd?: unknown;
      keys_present?: unknown;
      last_heartbeat?: unknown;
    }
  | null
  | undefined;

function finiteOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Decide what may be shown as the client's wallet balance.
 *
 * Fails closed on every uncertainty — no telemetry, keys withdrawn, an
 * unparseable figure, or a report that predates the first credential fetch all
 * resolve to "no number to show". The one thing that is NOT treated as
 * uncertainty is a genuine zero: an account that really holds nothing has been
 * read, and hiding that would be its own kind of lie.
 *
 * @param row      the user's executor_status telemetry
 * @param options  `keysConnected` from the user's own key metadata, used when
 *                 the executor has not reported yet
 * @param now      injectable clock, for tests
 */
export function resolveWalletDisplay(
  row: WalletSource,
  options?: { keysConnected?: boolean | null },
  now: number = Date.now(),
): WalletDisplay {
  const keysConnected = options?.keysConnected;

  // No executor has ever reported for this user.
  if (!row) {
    return keysConnected === true ? { state: "awaiting_read" } : { state: "not_connected" };
  }

  // The executor's view wins when it has one: it is the process that actually
  // tried to use the keys. A `false` here outranks a balance still sitting in
  // the column from before the client disconnected — that money is no longer
  // ours to report.
  if (row.keys_present === false) return { state: "not_connected" };

  // `keys_present === null` means "not yet known" (an OFF-mode report, or one
  // sent before the first credential fetch). Not a confirmation, so without
  // separate evidence this stays closed.
  if (row.keys_present !== true && keysConnected !== true) return { state: "not_connected" };

  const walletUsd = finiteOrNull(row.wallet_balance_usd);
  if (walletUsd === null) return { state: "awaiting_read" };

  const heartbeat = typeof row.last_heartbeat === "string" ? Date.parse(row.last_heartbeat) : NaN;
  const stale = !Number.isFinite(heartbeat) || now - heartbeat >= WALLET_FRESH_MS;

  return {
    state: "connected",
    walletUsd,
    availableUsd: finiteOrNull(row.available_balance_usd),
    stale,
  };
}
