import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

const Query = z.object({
  user_id: z.string().uuid(),
  symbol: z.string().min(1).max(30).optional(),
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
 * Exposes completed order metadata to the separate read-only Binance accounting
 * synchroniser, which needs to know which Binance order IDs opened and closed a
 * position. It writes nothing, and is not called by signal generation, sizing,
 * order placement or reconciliation.
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
            symbol: url.searchParams.get("symbol") ?? undefined,
            limit: url.searchParams.get("limit") ?? undefined,
          });
        } catch (e) {
          return new Response(JSON.stringify({ error: String(e) }), {
            status: 400,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        }

        const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
        // Ordered NEWEST first so `limit` truncates the distant past rather than
        // the recent trades the synchroniser is actually here to account for —
        // an ascending order with a limit would, on a long-lived account, return
        // only orders far older than any Binance fill lookback window.
        let query = supabaseAdmin
          .from("engine_orders")
          .select("side,intent,symbol,qty,binance_order_id,status,created_at")
          .eq("user_id", parsed.user_id)
          .in("status", ["SENT", "FILLED"])
          .not("binance_order_id", "is", null)
          .order("created_at", { ascending: false })
          .limit(parsed.limit);
        if (parsed.symbol) query = query.eq("symbol", parsed.symbol);

        const { data, error } = await query;

        if (error) {
          return new Response(JSON.stringify({ error: error.message }), {
            status: 500,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        }

        // Returned oldest-first: the synchroniser pairs each OPEN with the CLOSE
        // that follows it, which is only meaningful in chronological order.
        const orders = [...(data ?? [])].reverse();

        return new Response(JSON.stringify({ orders }), {
          status: 200,
          headers: { "Content-Type": "application/json", ...CORS },
        });
      },
    },
  },
});
