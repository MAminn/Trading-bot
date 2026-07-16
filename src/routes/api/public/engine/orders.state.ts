import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

const Query = z.object({
  user_id: z.string().uuid(),
});

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

function unauthorized() {
  return new Response("Unauthorized", { status: 401, headers: CORS });
}

export const Route = createFileRoute("/api/public/engine/orders/state")({
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
          });
        } catch (e) {
          return new Response(JSON.stringify({ error: String(e) }), {
            status: 400,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        }

        const { supabaseAdmin } = await import("@/integrations/supabase/client.server");

        const { data: lastExecuted, error: lastError } = await supabaseAdmin
          .from("engine_orders")
          .select("side, intent, qty, created_at")
          .eq("user_id", parsed.user_id)
          .in("status", ["SENT", "FILLED"])
          .order("created_at", { ascending: false })
          .limit(1)
          .maybeSingle();
        if (lastError)
          return new Response(JSON.stringify({ error: lastError.message }), {
            status: 500,
            headers: { "Content-Type": "application/json", ...CORS },
          });

        const staleBefore = new Date(Date.now() - 10 * 60 * 1000).toISOString();
        const { data: staleRows, error: staleError } = await supabaseAdmin
          .from("engine_orders")
          .select("idempotency_key")
          .eq("user_id", parsed.user_id)
          .eq("status", "INTENT_LOGGED")
          .is("binance_order_id", null)
          .lt("created_at", staleBefore)
          .order("created_at", { ascending: true })
          .limit(20);
        if (staleError)
          return new Response(JSON.stringify({ error: staleError.message }), {
            status: 500,
            headers: { "Content-Type": "application/json", ...CORS },
          });

        const { data: config, error: configError } = await supabaseAdmin
          .from("engine_config")
          .select("is_running")
          .eq("user_id", parsed.user_id)
          .maybeSingle();
        if (configError)
          return new Response(JSON.stringify({ error: configError.message }), {
            status: 500,
            headers: { "Content-Type": "application/json", ...CORS },
          });

        const body = {
          last_executed: lastExecuted ?? null,
          stale_intents: (staleRows ?? []).map((r) => r.idempotency_key),
          is_running: config?.is_running ?? true,
        };

        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json", ...CORS },
        });
      },
    },
  },
});
