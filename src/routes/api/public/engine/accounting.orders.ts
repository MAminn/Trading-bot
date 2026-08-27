import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

const Query = z.object({
  user_id: z.string().uuid(),
  limit: z.coerce.number().int().min(1).max(2000).default(500),
});

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

function unauthorized() {
  return new Response("Unauthorized", { status: 401, headers: CORS });
}

/**
 * Accounting-only order feed.
 *
 * This route exposes completed/sent order metadata to the separate read-only
 * Binance accounting synchronizer. It never writes trading state and is not
 * called by signal generation, sizing, placement or reconciliation.
 */
export const Route = createFileRoute("/api/public/engine/accounting/orders")({
  server: {
    handlers: {
      OPTIONS: async () => new Response(null, { status: 204, headers: CORS }),
      GET: async ({ request }) => {
        const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
        if (!token || token !== process.env.ENGINE_SERVICE_TOKEN) return unauthorized();

        const url = new URL(request.url);
        let parsed;
        try {
          parsed = Query.parse({
            user_id: url.searchParams.get("user_id") ?? undefined,
            limit: url.searchParams.get("limit") ?? undefined,
          });
        } catch (e) {
          return new Response(JSON.stringify({ error: String(e) }), {
            status: 400,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        }

        const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
        const { data, error } = await supabaseAdmin
          .from("engine_orders")
          .select("side,intent,symbol,qty,binance_order_id,status,created_at")
          .eq("user_id", parsed.user_id)
          .in("status", ["SENT", "FILLED"])
          .not("binance_order_id", "is", null)
          .order("created_at", { ascending: true })
          .limit(parsed.limit);

        if (error) {
          return new Response(JSON.stringify({ error: error.message }), {
            status: 500,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        }

        return new Response(JSON.stringify({ orders: data ?? [] }), {
          status: 200,
          headers: { "Content-Type": "application/json", ...CORS },
        });
      },
    },
  },
});
