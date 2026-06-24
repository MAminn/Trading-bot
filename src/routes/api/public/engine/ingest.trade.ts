import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

const Body = z.object({
  user_id: z.string().uuid(),
  trade_id: z.string().max(100).optional(),
  side: z.string().max(20).optional(),
  setup_name: z.string().max(100).optional(),
  signal_t: z.string().datetime().optional(),
  entry_t: z.string().datetime().optional(),
  exit_t: z.string().datetime().optional(),
  entry: z.number().optional(),
  exit: z.number().optional(),
  tp: z.number().optional(),
  sl: z.number().optional(),
  final_stop: z.number().optional(),
  atr: z.number().optional(),
  bars_held: z.number().int().optional(),
  prob: z.number().optional(),
  threshold: z.number().optional(),
  exit_reason: z.string().max(200).optional(),
  net_pnl_rate: z.number().optional(),
  round_trip_cost: z.number().optional(),
});

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

export const Route = createFileRoute("/api/public/engine/ingest/trade")({
  server: {
    handlers: {
      OPTIONS: async () => new Response(null, { status: 204, headers: CORS }),
      POST: async ({ request }) => {
        const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
        if (!token || token !== process.env.ENGINE_SERVICE_TOKEN) return new Response("Unauthorized", { status: 401, headers: CORS });
        let parsed;
        try { parsed = Body.parse(await request.json()); }
        catch (e) { return new Response(JSON.stringify({ error: String(e) }), { status: 400, headers: { "Content-Type": "application/json", ...CORS } }); }
        const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
        const { error } = await supabaseAdmin.from("user_trades").insert(parsed as never);
        if (error) return new Response(JSON.stringify({ error: error.message }), { status: 500, headers: { "Content-Type": "application/json", ...CORS } });
        return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json", ...CORS } });
      },
    },
  },
});
