// Who the engine acts for. Server-only.
//
// Two different questions, deliberately answered separately, because collapsing
// them is how a stopped client silently disappears from their own dashboard:
//
//   SIGNAL SUBSCRIBERS  — who receives a copy of each strategy signal.
//                         Gated on `is_running`: a client who has never pressed
//                         Start gets no signal rows at all.
//
//   EXECUTION ROSTER    — who the executor builds a session for.
//                         Gated on `execution_mode`, NOT on `is_running`. A
//                         client who presses Stop stays on the roster so their
//                         executor keeps heartbeating and the Engine page shows
//                         `kill_switch_active` rather than a stale heartbeat.
//                         Being on the roster grants attention, never
//                         capability: every gate is still re-evaluated per
//                         cycle, per user, by the executor itself.
//
// Neither function returns, reads, or can be made to return key material. The
// roster carries user ids and a presence boolean and nothing else.

// The executor builds one session per roster entry, each with its own Binance
// client and its own credentials fetch. An unbounded roster would be an
// unbounded number of exchange connections from one host, so it is capped and
// the cap is reported rather than silently applied.
export const MAX_ROSTER_USERS = 500;

// Modes a user can ask for. Mirrors engine_config's CHECK constraint; anything
// else is treated as OFF rather than guessed at.
export const EXECUTABLE_MODES = ["LIVE_READ", "LIVE_TRADE"] as const;

export type ConfigRow = {
  user_id: unknown;
  execution_mode?: unknown;
  is_running?: unknown;
  demo_mode?: unknown;
};

function validUserId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return /^[0-9a-f-]{36}$/i.test(trimmed) ? trimmed : null;
}

/**
 * Does this config ask for live execution?
 *
 * Fail-closed on every uncertainty: a missing column (a row written before the
 * live-controls migration), an unrecognised string, or a non-string all mean
 * "no". Uncertainty about what was requested is never resolved as permission.
 *
 * `demo_mode` is excluded explicitly even though the database CHECK already
 * forbids demo_mode with a live execution_mode. The constraint is the
 * authority; this is the second lock, because demo signals are fabricated and
 * an executor consuming them would place real orders against invented data.
 */
export function isExecutable(row: ConfigRow): boolean {
  if (row.demo_mode === true) return false;
  const mode = typeof row.execution_mode === "string" ? row.execution_mode.trim() : "";
  return (EXECUTABLE_MODES as readonly string[]).includes(mode);
}

/**
 * Should this user receive a copy of each strategy signal?
 *
 * `is_running` is the client's own Start/Stop. A stopped client stops
 * accumulating signals; they do not stop existing.
 */
export function isSignalSubscriber(row: ConfigRow): boolean {
  if (row.demo_mode === true) return false;
  return row.is_running === true;
}

/**
 * Turn config rows into the executor's roster: deduplicated, validated, capped.
 *
 * Pure, so the eligibility rules can be tested exhaustively without a database.
 */
export function selectRoster(rows: ConfigRow[]): {
  userIds: string[];
  truncated: boolean;
} {
  const seen = new Set<string>();
  for (const row of rows) {
    if (!isExecutable(row)) continue;
    const id = validUserId(row.user_id);
    // A malformed id cannot be executed against and must not be passed on: the
    // executor would build a session for a user that cannot be looked up.
    if (id === null || seen.has(id)) continue;
    seen.add(id);
  }
  const all = [...seen];
  return {
    userIds: all.slice(0, MAX_ROSTER_USERS),
    truncated: all.length > MAX_ROSTER_USERS,
  };
}

/** Users who should receive a copy of a broadcast signal. */
export function selectSignalSubscribers(rows: ConfigRow[]): string[] {
  const seen = new Set<string>();
  for (const row of rows) {
    if (!isSignalSubscriber(row)) continue;
    const id = validUserId(row.user_id);
    if (id === null) continue;
    seen.add(id);
  }
  return [...seen];
}

/** Fetch the execution roster. */
export async function loadExecutionRoster(): Promise<{
  userIds: string[];
  truncated: boolean;
}> {
  const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
  const { data, error } = await supabaseAdmin
    .from("engine_config")
    .select("user_id,execution_mode,is_running,demo_mode")
    .in("execution_mode", EXECUTABLE_MODES as unknown as string[]);
  if (error) throw new Error(error.message);
  return selectRoster((data ?? []) as ConfigRow[]);
}

/** Fetch the users a broadcast signal should be copied to. */
export async function loadSignalSubscribers(): Promise<string[]> {
  const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
  const { data, error } = await supabaseAdmin
    .from("engine_config")
    .select("user_id,execution_mode,is_running,demo_mode")
    .eq("is_running", true);
  if (error) throw new Error(error.message);
  return selectSignalSubscribers((data ?? []) as ConfigRow[]);
}
