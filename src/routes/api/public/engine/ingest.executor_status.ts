// The Binance executor posts its own telemetry here every cycle. Token-protected.
//
// Separate from /heartbeat, which the ML signal worker owns: that endpoint
// writes engine_status (the strategy's paper position), this one writes
// executor_status (the exchange's real position and balances).
//
// Telemetry sink only — nothing written here feeds back into execution.
import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

const MODES = ["OFF", "TESTNET_READ", "TESTNET_TRADE", "LIVE_READ", "LIVE_TRADE"] as const;

const Body = z.object({
  user_id: z.string().uuid(),
  effective_mode: z.enum(MODES),
  env_mode_ceiling: z.enum(MODES).optional(),
  // Every numeric is nullable, not just optional: the executor reports an
  // unknown balance/position as null rather than inventing a zero.
  wallet_balance_usd: z.number().nullable().optional(),
  available_balance_usd: z.number().nullable().optional(),
  position_amt: z.number().nullable().optional(),
  position_side: z.enum(["FLAT", "LONG", "SHORT"]).nullable().optional(),
  entry_price: z.number().nullable().optional(),
  position_leverage: z.number().nullable().optional(),
  margin_type: z.string().max(30).nullable().optional(),
  reconcile_match: z.boolean().nullable().optional(),
  reconcile_expected: z.number().nullable().optional(),
  reconcile_actual: z.number().nullable().optional(),
  last_reconcile_at: z.string().datetime().nullable().optional(),
  keys_present: z.boolean().nullable().optional(),
  permission_status: z.enum(["verified_futures", "unknown", "failed"]).nullable().optional(),
  message: z.string().max(500).nullable().optional(),
  // Live-control telemetry (Phase 3). These MUST be listed here: z.object()
  // strips unknown keys, so a field the executor sends but this schema omits is
  // discarded silently — the row simply keeps its old value and nothing errors.
  // That is exactly how these six arrived as null while effective_mode,
  // env_mode_ceiling and message (the fields that were listed) came through.
  db_execution_mode: z.enum(["OFF", "LIVE_READ", "LIVE_TRADE"]).nullable().optional(),
  auto_execute_enabled: z.boolean().nullable().optional(),
  orders_enabled: z.boolean().nullable().optional(),
  blocked_reason: z.string().max(200).nullable().optional(),
});

// Every telemetry key the executor sends. Kept beside the schema so the drift
// that caused the Phase 3 null columns is a test failure rather than silent
// data loss. Asserted against Body's own key list below.
export const EXECUTOR_STATUS_FIELDS = Object.keys(Body.shape).filter((k) => k !== "user_id");

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

function unauthorized() {
  return new Response("Unauthorized", { status: 401, headers: CORS });
}

export const Route = createFileRoute("/api/public/engine/ingest/executor_status")({
  server: {
    handlers: {
      OPTIONS: async () => new Response(null, { status: 204, headers: CORS }),
      POST: async ({ request }) => {
        const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
        if (!token || token !== process.env.ENGINE_SERVICE_TOKEN) return unauthorized();
        let parsed;
        try {
          parsed = Body.parse(await request.json());
        } catch (e) {
          return new Response(JSON.stringify({ error: String(e) }), {
            status: 400,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        }
        const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
        const now = new Date().toISOString();
        const { error } = await supabaseAdmin.from("executor_status").upsert(
          {
            ...parsed,
            last_heartbeat: now,
            updated_at: now,
          } as never,
          { onConflict: "user_id" },
        );
        if (error)
          return new Response(JSON.stringify({ error: error.message }), {
            status: 500,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json", ...CORS },
        });
      },
    },
  },
});
