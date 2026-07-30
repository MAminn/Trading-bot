// Server functions for the authenticated user to control their engine.
import { createServerFn } from "@tanstack/react-start";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
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

// Allocation is the single source of truth; leverage is derived from it.
// These are the only ten pairs the UI can produce and the only ten accepted.
const LEVERAGE_BY_ALLOC: Record<number, number> = {
  10: 1, 9: 10, 8: 20, 7: 30, 6: 40, 5: 50, 4: 60, 3: 70, 2: 80, 1: 90,
};

const ConfigPatch = z.object({
  capital_usd: z.number().positive().max(1e9).optional(),
  // allocation is capped at 10% to keep position size small relative to account
  capital_allocation_pct: z.number().min(1).max(10).optional(),
  leverage: z.number().min(1).max(90).optional(),
  max_notional_usd: z.number().positive().max(100000).optional(),
  // these two fields are reused as Take-Profit % and Stop-Loss % in the UI.
  // Numeric ranges remain wide so older values continue to validate.
  max_daily_loss_usd: z.number().min(0).max(1e9).optional(),
  max_position_size_usd: z.number().min(0).max(1e9).optional(),
  demo_mode: z.boolean().optional(),
  // mode is locked to signal_only for now
  mode: z.enum(["signal_only"]).optional(),
}).superRefine((val, ctx) => {
  const hasLev = val.leverage !== undefined;
  const hasAlloc = val.capital_allocation_pct !== undefined;
  // Neither present: unrelated patches (e.g. demo_mode toggle) pass untouched.
  if (!hasLev && !hasAlloc) return;
  if (hasLev !== hasAlloc) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: [hasLev ? "capital_allocation_pct" : "leverage"],
      message: "leverage and capital_allocation_pct must be updated together",
    });
    return;
  }
  const expected = LEVERAGE_BY_ALLOC[val.capital_allocation_pct as number];
  if (expected === undefined || expected !== val.leverage) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["leverage"],
      message:
        `leverage ${val.leverage} does not match allocation ` +
        `${val.capital_allocation_pct}% (expected ${expected ?? "a whole 1-10% allocation"})`,
    });
  }
});

export const updateEngineConfig = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .inputValidator((d: unknown) => ConfigPatch.parse(d))
  .handler(async ({ data, context }) => {
    if (Object.keys(data).length === 0) return { ok: true };
    const { error } = await context.supabase
      .from("engine_config")
      // max_notional_usd is newer than the generated types; same `as never`
      // escape hatch the other engine writes already use.
      .update({ ...data, updated_at: new Date().toISOString() } as never)
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
