import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

const Body = z.object({
  user_id: z.string().uuid(),
  signal_bar_time: z.string().datetime(),
  symbol: z.string().max(20).optional(),
  side: z.enum(["LONG", "SHORT"]),
  intent: z.enum(["OPEN", "CLOSE"]),
  qty: z.number(),
  ref_price: z.number().optional(),
  notional_usd: z.number().optional(),
  execution_mode: z.string().max(50),
  status: z.enum(["INTENT_LOGGED", "DRYRUN", "SENT", "FILLED", "FAILED", "SKIPPED"]).optional(),
  idempotency_key: z.string().max(200),
  binance_order_id: z.string().max(100).optional(),
  error: z.string().max(1000).optional(),
});

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

function unauthorized() {
  return new Response("Unauthorized", { status: 401, headers: CORS });
}

export const Route = createFileRoute("/api/public/engine/ingest/order")({
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
        const { data, error } = await supabaseAdmin
          .from("engine_orders")
          .insert(parsed as never)
          .select("id")
          .single();
        if (error) {
          if (error.code === "23505")
            return new Response(JSON.stringify({ error: "duplicate" }), {
              status: 409,
              headers: { "Content-Type": "application/json", ...CORS },
            });
          return new Response(JSON.stringify({ error: error.message }), {
            status: 500,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        }
        return new Response(JSON.stringify({ id: (data as { id: string }).id }), {
          status: 201,
          headers: { "Content-Type": "application/json", ...CORS },
        });
      },
    },
  },
});
