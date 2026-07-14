import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

const Body = z.object({
  user_id: z.string().uuid(),
  idempotency_key: z.string().max(200),
  status: z.enum(["SENT", "FILLED", "FAILED"]),
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

export const Route = createFileRoute("/api/public/engine/ingest/order_update")({
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
        const { user_id, idempotency_key, ...updates } = parsed;
        const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
        const { data, error } = await supabaseAdmin
          .from("engine_orders")
          .update(updates as never)
          .eq("idempotency_key", idempotency_key)
          .eq("user_id", user_id)
          .select("id")
          .maybeSingle();
        if (error) {
          return new Response(JSON.stringify({ error: error.message }), {
            status: 500,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        }
        if (!data) {
          return new Response(JSON.stringify({ error: "not_found" }), {
            status: 404,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        }
        return new Response(JSON.stringify({ id: (data as { id: string }).id }), {
          status: 200,
          headers: { "Content-Type": "application/json", ...CORS },
        });
      },
    },
  },
});
