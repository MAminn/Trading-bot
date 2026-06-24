// Built-in V22 engine tick.
//
// This is a TypeScript port of the frozen V22 LONG/SHORT rule engine
// (see src/lib/v22-engine.ts for the constants). It uses REAL 15m ETHUSDT
// klines from Binance, computes ATR(14) and the V22 expansion features
// on the most recently closed 15m bar, gates the signal through an
// ML-proxy probability vs the locked thresholds (long=0.40, short=0.39),
// and manages the resulting position with V22 stops: TP=1.7×ATR,
// SL=1.35×ATR, trail at 0.65×ATR, max hold 24 bars (6h).
//
// Runs every minute via pg_cron, but only EVALUATES signals once per
// closed 15m bar (engine_status.message stores the last evaluated bar
// open time as `bar:<ms>`). Between bars it only updates trailing stops
// and exits on TP/SL/time.
import { createFileRoute } from "@tanstack/react-router";
import {
  V22, fetchKlines, features, ruleSide, mlProxy, buildLevels, trailStop, type Kline,
} from "@/lib/v22-engine";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};
const SETUP = "v22_expansion";
const BAR_MS = 15 * 60 * 1000;

export const Route = createFileRoute("/api/public/engine/demo-tick")({
  server: {
    handlers: {
      OPTIONS: async () => new Response(null, { status: 204, headers: CORS }),
      POST: async ({ request }) => {
        const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
        const apikey = request.headers.get("apikey") ?? request.headers.get("x-api-key");
        const okBearer = !!token && token === process.env.ENGINE_SERVICE_TOKEN;
        const okApikey = !!apikey && (apikey === process.env.SUPABASE_PUBLISHABLE_KEY || apikey === process.env.SUPABASE_ANON_KEY);
        if (!okBearer && !okApikey) return new Response("Unauthorized", { status: 401, headers: CORS });

        const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
        const { data: users, error: usersErr } = await supabaseAdmin
          .from("engine_config").select("user_id").eq("is_running", true);
        if (usersErr)
          return new Response(JSON.stringify({ error: usersErr.message }), { status: 500, headers: { "Content-Type": "application/json", ...CORS } });

        // Fetch klines once for all users
        let ks: Kline[] = [];
        try { ks = await fetchKlines(50); } catch (e) {
          return new Response(JSON.stringify({ error: `klines: ${String(e)}` }), { status: 502, headers: { "Content-Type": "application/json", ...CORS } });
        }
        // The LAST kline from Binance is the in-progress bar. The last
        // CLOSED bar is ks[ks.length - 2].
        const closedBars = ks.slice(0, -1);
        const livePrice = ks[ks.length - 1]?.close ?? closedBars[closedBars.length - 1].close;
        const f = features(closedBars);
        const rule = ruleSide(f);
        const nowIso = new Date().toISOString();

        let processed = 0;
        for (const row of users ?? []) {
          const userId = (row as { user_id: string }).user_id;
          try {
            await tickUser(supabaseAdmin, userId, livePrice, f, rule, nowIso);
            processed++;
          } catch (e) { console.error("v22-tick user failed", userId, e); }
        }
        return new Response(JSON.stringify({ ok: true, processed, eth: livePrice, bar: new Date(f.barTime).toISOString(), rule: rule.side }), {
          status: 200, headers: { "Content-Type": "application/json", ...CORS },
        });
      },
    },
  },
});

async function tickUser(
  // deno-lint-ignore no-explicit-any
  db: any,
  userId: string,
  livePrice: number,
  f: ReturnType<typeof features>,
  rule: ReturnType<typeof ruleSide>,
  nowIso: string,
) {
  // Check if this 15m bar was already evaluated for this user
  const { data: status } = await db.from("engine_status").select("message").eq("user_id", userId).maybeSingle();
  const lastBarTag = `bar:${f.barTime}`;
  const newBar = (status?.message ?? "") !== lastBarTag;

  // Current open position (this setup only — engine manages one at a time)
  const { data: openRows } = await db.from("open_positions").select("*").eq("user_id", userId).eq("setup_name", SETUP);
  const open = (openRows ?? [])[0];

  type Pos = "FLAT" | "LONG" | "SHORT";
  const positionBefore: Pos = (open?.side as Pos) ?? "FLAT";
  let positionAfter: Pos = positionBefore;
  let opened: string | null = null;
  let closedReason: string | null = null;
  let signalTradeId: string | null = open?.trade_id ?? null;
  let mlProb = 0;
  const mlThreshold = rule.side === 1 ? V22.ml.long_threshold : rule.side === -1 ? V22.ml.short_threshold : 0;
  let mlAccept = false;

  // ============ Position management (every minute) ============
  if (open) {
    const entry = Number(open.entry);
    const side: 1 | -1 = (open.side ?? "LONG").toUpperCase() === "LONG" ? 1 : -1;
    const atrPx = Number(open.atr ?? 0);
    const tp = Number(open.tp);
    let stop = Number(open.current_stop ?? open.sl);
    const barsHeld = Number(open.bars_held ?? 0) + (newBar ? 1 : 0);

    // Trail stop using live price (V22 trail)
    if (atrPx > 0) stop = trailStop(entry, atrPx, side, stop, livePrice);

    const hitTp = side === 1 ? livePrice >= tp : livePrice <= tp;
    const hitSl = side === 1 ? livePrice <= stop : livePrice >= stop;
    const timeOut = barsHeld >= V22.mgmt.max_hold_bars;

    if (hitTp || hitSl || timeOut) {
      closedReason = hitTp ? "tp" : hitSl ? "sl" : "max_hold";
      const exitPrice = hitTp ? tp : hitSl ? stop : livePrice;
      const netRate = side === 1
        ? (exitPrice - entry) / entry - 0.0008
        : (entry - exitPrice) / entry - 0.0008;
      await db.from("user_trades").insert({
        user_id: userId, trade_id: open.trade_id, side: side === 1 ? "LONG" : "SHORT",
        setup_name: SETUP, signal_t: open.entry_t, entry_t: open.entry_t, exit_t: nowIso,
        entry, exit: exitPrice, tp, sl: Number(open.sl), final_stop: stop, atr: atrPx,
        bars_held: barsHeld, prob: open.prob, threshold: open.threshold,
        exit_reason: closedReason, net_pnl_rate: Number(netRate.toFixed(5)), round_trip_cost: 0.0008,
      });
      await db.from("open_positions").delete().eq("id", open.id);
      positionAfter = "FLAT";
    } else {
      const unrealRate = side === 1 ? (livePrice - entry) / entry : (entry - livePrice) / entry;
      await db.from("open_positions").update({
        bars_held: barsHeld, current_stop: Number(stop.toFixed(2)),
        unrealized_pnl_rate: Number(unrealRate.toFixed(5)), updated_at: nowIso,
      }).eq("id", open.id);
    }
  }

  // ============ Signal evaluation (only on new closed 15m bar) ============
  if (newBar && positionAfter === "FLAT" && rule.side !== 0) {
    // V22 no_flip: ignore opposite signals while a position is open — already
    // enforced by the FLAT check above.
    mlProb = Number(mlProxy(f, rule.side).toFixed(3));
    mlAccept = mlProb >= mlThreshold;
    if (mlAccept) {
      // Entry = next 15m open ≈ current live price (we already crossed the bar)
      const entry = Number(livePrice.toFixed(2));
      const atrPx = Number(f.atr.toFixed(2));
      const { tp, sl } = buildLevels(entry, atrPx, rule.side);
      const tradeId = `v22-${f.barTime}-${userId.slice(0, 8)}`;
      await db.from("open_positions").upsert({
        user_id: userId, trade_id: tradeId,
        side: rule.side === 1 ? "LONG" : "SHORT",
        setup_name: SETUP, entry_t: nowIso, entry, sl: Number(sl.toFixed(2)), tp: Number(tp.toFixed(2)),
        current_stop: Number(sl.toFixed(2)), atr: atrPx, bars_held: 0,
        prob: mlProb, threshold: mlThreshold, unrealized_pnl_rate: 0, updated_at: nowIso,
      }, { onConflict: "user_id,trade_id" });
      positionAfter = rule.side === 1 ? "LONG" : "SHORT";
      opened = positionAfter;
      signalTradeId = tradeId;
    }
  }

  // Always write a signal row on a new bar (and also on close events) so the
  // dashboard timeline reflects every evaluated bar.
  if (newBar || closedReason) {
    await db.from("user_signals").insert({
      user_id: userId,
      bar_time: new Date(f.barTime).toISOString(),
      bar_closed_now: true,
      valid_next_entry: rule.side !== 0 && mlAccept,
      rule_side: closedReason ? 0 : rule.side,
      rule_reason: closedReason ? `close: ${closedReason}` : rule.reason,
      ml_prob: rule.side !== 0 ? mlProb : null,
      ml_threshold: rule.side !== 0 ? mlThreshold : null,
      ml_accept: rule.side !== 0 ? mlAccept : null,
      opened, closed_reason: closedReason,
      position_before: positionBefore, position_after: positionAfter,
      trade_id: signalTradeId,
    });
  }

  // Heartbeat — stamp the last evaluated bar in `message` so we only fire
  // one signal row per closed 15m bar.
  await db.from("engine_status").upsert({
    user_id: userId, status: "running", current_position: positionAfter,
    message: lastBarTag, last_heartbeat: nowIso, updated_at: nowIso,
  }, { onConflict: "user_id" });
}
