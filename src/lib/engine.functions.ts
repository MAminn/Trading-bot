// Server functions for the authenticated user to control their engine.
import { createServerFn } from "@tanstack/react-start";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import {
  LEVERAGE_BY_ALLOC,
  LEVERAGE_MAX,
  LEVERAGE_MIN,
  SIZING_MODES,
  capitalFromAllocation,
} from "@/lib/sizing";
import {
  LIVE_CONTROL_TUPLE,
  LIVE_ORDER_CAP_MAX_USD,
  LIVE_STATE_INPUTS,
  PRIVILEGED_CONFIG_FIELDS,
  REQUESTED_EXECUTION_MODES,
  formatViolations,
  validateLiveState,
} from "@/lib/live-controls";
import { z } from "zod";

export const setEngineRunning = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d: { running: boolean }) => z.object({ running: z.boolean() }).parse(d))
  .handler(async ({ data, context }) => {
    const nowIso = new Date().toISOString();
    const { data: updated, error } = await context.supabase
      .from("engine_config")
      .update({ is_running: data.running, updated_at: nowIso })
      .eq("user_id", context.userId)
      .select("id");
    if (error || !updated || updated.length === 0) {
      throw new Error("engine_config update affected 0 rows — wrong account or missing config row");
    }
    // Immediately reflect the desired state in engine_status so the UI pill
    // changes the moment the user presses Start/Stop, before the first tick.
    const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
    await supabaseAdmin.from("engine_status").upsert(
      {
        user_id: context.userId,
        status: data.running ? "running" : "stopped",
        message: data.running ? "starting…" : "stopped by user",
        last_heartbeat: null,
        updated_at: nowIso,
      },
      { onConflict: "user_id" },
    );
    return { ok: true };
  });

// Every field that participates in sizing. If a patch touches any of them it
// must declare the mode it is sizing under, so no patch can mutate sizing into
// a state this validator never evaluated.
const SIZING_FIELDS = [
  "sizing_mode",
  "account_size_usd",
  "capital_usd",
  "capital_allocation_pct",
  "leverage",
] as const;

export const ConfigPatch = z.object({
  sizing_mode: z.enum(SIZING_MODES).optional(),
  account_size_usd: z.number().positive().max(1e9).optional(),
  capital_usd: z.number().positive().max(1e9).optional(),
  // allocation is capped at 10% to keep position size small relative to account
  capital_allocation_pct: z.number().min(1).max(10).optional(),
  leverage: z.number().min(LEVERAGE_MIN).max(LEVERAGE_MAX).optional(),
  max_notional_usd: z.number().positive().max(100000).optional(),
  // Legacy columns, no longer written by the UI. Ranges stay wide so older
  // values continue to validate.
  max_daily_loss_usd: z.number().min(0).max(1e9).optional(),
  max_position_size_usd: z.number().min(0).max(1e9).optional(),
  demo_mode: z.boolean().optional(),
  // Auto-execute. The DB derives auto_execute_enabled from this column, so it
  // is the single switch — there is no separate boolean to disagree with it.
  mode: z.enum(["signal_only", "auto"]).optional(),
  // ----- live execution controls (privileged; service-role write only) -----
  execution_mode: z.enum(REQUESTED_EXECUTION_MODES).optional(),
  live_order_cap_usd: z.number().min(0).max(LIVE_ORDER_CAP_MAX_USD).optional(),
  live_allow_full_capital: z.boolean().optional(),
}).superRefine((val, ctx) => {
  const reject = (path: string, message: string) =>
    ctx.addIssue({ code: z.ZodIssueCode.custom, path: [path], message });

  // ----- live execution controls -----
  // Same principle as the sizing block below: validating execution_mode
  // without the cap it will be paired with is not validation, so a patch
  // touching any of the tuple must declare all of it.
  const liveTouched = LIVE_CONTROL_TUPLE.filter((f) => val[f] !== undefined);
  if (liveTouched.length > 0) {
    const missing = LIVE_CONTROL_TUPLE.filter((f) => val[f] === undefined);
    if (missing.length > 0) {
      for (const f of missing) {
        reject(
          f,
          `${f} is required when updating ${liveTouched.join(", ")} — a ` +
            `live-execution patch must declare the whole control set`,
        );
      }
    } else {
      // Cross-field rules, evaluated on whatever this patch fully determines.
      // The handler re-runs the same function against the merged row, so
      // fields absent here (sizing_mode, demo_mode) are still checked before
      // the write — and the DB CHECKs remain the final authority.
      for (const v of validateLiveState(val)) reject(v.field, v.message);
    }
  }

  const touched = SIZING_FIELDS.filter((f) => val[f] !== undefined);
  // Unrelated patches (e.g. a demo_mode toggle) pass through untouched.
  if (touched.length === 0) return;

  if (val.sizing_mode === undefined) {
    reject(
      "sizing_mode",
      `sizing_mode is required when updating ${touched.join(", ")} — a sizing ` +
        `patch must declare the mode it applies to`,
    );
    return;
  }

  if (val.sizing_mode === "allocation") {
    const missing = (
      ["capital_allocation_pct", "leverage", "capital_usd", "account_size_usd"] as const
    ).filter((f) => val[f] === undefined);
    if (missing.length > 0) {
      for (const f of missing) {
        reject(f, `${f} is required when sizing_mode is allocation`);
      }
      return;
    }
    const pct = val.capital_allocation_pct as number;
    const expected = LEVERAGE_BY_ALLOC[pct];
    if (expected === undefined || expected !== val.leverage) {
      reject(
        "leverage",
        `leverage ${val.leverage} does not match allocation ${pct}% ` +
          `(expected ${expected ?? "a whole 1-10% allocation"})`,
      );
    }
    const expectedCapital = capitalFromAllocation(val.account_size_usd as number, pct);
    if (val.capital_usd !== expectedCapital) {
      reject(
        "capital_usd",
        `capital_usd ${val.capital_usd} does not match ${pct}% of account size ` +
          `${val.account_size_usd} (expected ${expectedCapital})`,
      );
    }
    return;
  }

  // full_capital: sized off account_size_usd directly.
  if (val.capital_allocation_pct !== undefined) {
    // Meaningless here; writing it would leave a misleading row behind.
    reject(
      "capital_allocation_pct",
      "capital_allocation_pct must not be sent when sizing_mode is full_capital",
    );
  }
  if (val.account_size_usd === undefined) {
    reject("account_size_usd", "account_size_usd is required when sizing_mode is full_capital");
    return;
  }
  if (val.capital_usd === undefined || val.capital_usd !== val.account_size_usd) {
    reject(
      "capital_usd",
      `capital_usd must equal account_size_usd ${val.account_size_usd} in ` +
        `full_capital mode (got ${val.capital_usd})`,
    );
  }
  // leverage stays free within 1-90 here: it is NOT derived from allocation.
});

export const updateEngineConfig = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d: unknown) => ConfigPatch.parse(d))
  .handler(async ({ data, context }) => {
    if (Object.keys(data).length === 0) return { ok: true };

    // Migration 20260817131000 revoked UPDATE on these columns from
    // `authenticated`, so RLS is no longer what protects them — this function
    // is. A patch touching any of them is written with the service role, and
    // the row is selected by the user id from the verified JWT claims, never
    // from anything the caller supplied. (ConfigPatch is a plain z.object, so
    // an injected user_id is stripped before it reaches here.)
    const touchesPrivileged = PRIVILEGED_CONFIG_FIELDS.some((f) => data[f] !== undefined);
    // Any input to the live-execution invariants forces a merged-state check:
    // a patch that flips demo_mode alone, or sizing_mode alone, can still
    // produce a forbidden combination with columns it does not mention.
    const touchesLiveState = LIVE_STATE_INPUTS.some((f) => data[f] !== undefined);

    const nowIso = new Date().toISOString();

    if (touchesLiveState || touchesPrivileged) {
      const { supabaseAdmin } = await import("@/integrations/supabase/client.server");

      const { data: current, error: readErr } = await supabaseAdmin
        .from("engine_config")
        .select(
          "execution_mode, live_order_cap_usd, live_allow_full_capital, sizing_mode, demo_mode",
        )
        .eq("user_id", context.userId)
        .maybeSingle();
      if (readErr) throw new Error(readErr.message);
      if (!current) throw new Error("no engine_config row for this account");

      // Validate the state the row will END UP in, not the patch in isolation.
      const violations = validateLiveState({ ...current, ...data });
      if (violations.length > 0) throw new Error(formatViolations(violations));

      const db = touchesPrivileged ? supabaseAdmin : context.supabase;
      const { error } = await db
        .from("engine_config")
        .update({ ...data, updated_at: nowIso })
        .eq("user_id", context.userId);
      if (error) throw new Error(error.message);
      return { ok: true };
    }

    const { error } = await context.supabase
      .from("engine_config")
      // The generated types now carry every engine_config column this patch can
      // write, so the previous `as never` escape hatch is no longer needed.
      .update({ ...data, updated_at: nowIso })
      .eq("user_id", context.userId);
    if (error) throw new Error(error.message);
    return { ok: true };
  });

// ----- Manual position close (single + all) -----

async function fetchEthPrice(): Promise<number> {
  try {
    const r = await fetch("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT", {
      headers: { accept: "application/json" },
    });
    if (!r.ok) return 0;
    const j = (await r.json()) as { price?: string };
    const p = Number(j.price);
    return Number.isFinite(p) && p > 0 ? p : 0;
  } catch {
    return 0;
  }
}

async function closeOne(
  // deno-lint-ignore no-explicit-any
  db: any,
  userId: string,
  // deno-lint-ignore no-explicit-any
  open: any,
  ethPrice: number,
  nowIso: string,
) {
  const entry = Number(open.entry ?? ethPrice);
  const isLong = (open.side ?? "LONG").toUpperCase() === "LONG";
  const exitPrice = ethPrice || entry;
  const netRate = isLong
    ? (exitPrice - entry) / entry - 0.0008
    : (entry - exitPrice) / entry - 0.0008;

  await db.from("user_trades").insert({
    user_id: userId,
    trade_id: open.trade_id,
    side: isLong ? "LONG" : "SHORT",
    setup_name: open.setup_name,
    signal_t: open.entry_t,
    entry_t: open.entry_t,
    exit_t: nowIso,
    entry,
    exit: exitPrice,
    tp: open.tp,
    sl: open.sl,
    final_stop: open.current_stop,
    atr: open.atr,
    bars_held: open.bars_held,
    prob: open.prob,
    threshold: open.threshold,
    exit_reason: "manual_close",
    net_pnl_rate: Number(netRate.toFixed(5)),
    round_trip_cost: 0.0008,
  });
  await db.from("open_positions").delete().eq("id", open.id);
  await db.from("user_signals").insert({
    user_id: userId,
    bar_time: nowIso,
    bar_closed_now: true,
    valid_next_entry: false,
    rule_side: 0,
    rule_reason: "manual close",
    ml_prob: open.prob,
    ml_threshold: open.threshold,
    ml_accept: true,
    opened: null,
    closed_reason: "manual_close",
    position_before: isLong ? "LONG" : "SHORT",
    position_after: "FLAT",
    trade_id: open.trade_id,
  });
}

export const closePosition = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d: { id: string }) => z.object({ id: z.string().uuid() }).parse(d))
  .handler(async ({ data, context }) => {
    const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
    const { data: open, error } = await supabaseAdmin
      .from("open_positions")
      .select("*")
      .eq("id", data.id)
      .eq("user_id", context.userId)
      .maybeSingle();
    if (error) throw new Error(error.message);
    if (!open) throw new Error("Position not found");
    const ethPrice = await fetchEthPrice();
    const nowIso = new Date().toISOString();
    await closeOne(supabaseAdmin, context.userId, open, ethPrice, nowIso);
    return { ok: true };
  });

export const closeAllPositions = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }) => {
    const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
    const { data: opens, error } = await supabaseAdmin
      .from("open_positions")
      .select("*")
      .eq("user_id", context.userId);
    if (error) throw new Error(error.message);
    const ethPrice = await fetchEthPrice();
    const nowIso = new Date().toISOString();
    let closed = 0;
    for (const o of opens ?? []) {
      try {
        await closeOne(supabaseAdmin, context.userId, o, ethPrice, nowIso);
        closed++;
      } catch {
        /* ignore */
      }
    }
    return { ok: true, closed };
  });
