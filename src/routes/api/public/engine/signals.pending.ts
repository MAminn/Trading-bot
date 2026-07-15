import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

const Query = z.object({
  user_id: z.string().uuid(),
  after: z.string().datetime().optional(),
});

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

function unauthorized() {
  return new Response("Unauthorized", { status: 401, headers: CORS });
}

export const Route = createFileRoute("/api/public/engine/signals/pending")({
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
            after: url.searchParams.get("after") ?? undefined,
          });
        } catch (e) {
          return new Response(JSON.stringify({ error: String(e) }), {
            status: 400,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        }
        const after = parsed.after ?? new Date(Date.now() - 30 * 60 * 1000).toISOString();
        const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
        const { data, error } = await supabaseAdmin
          .from("user_signals")
          .select(
            "id, bar_time, rule_side, ml_accept, opened, closed_reason, position_before, position_after, created_at",
          )
          .eq("user_id", parsed.user_id)
          .or("rule_side.in.(1,-1),closed_reason.not.is.null")
          .gt("created_at", after)
          .order("created_at", { ascending: true })
          .limit(20);
        if (error)
          return new Response(JSON.stringify({ error: error.message }), {
            status: 500,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        return new Response(JSON.stringify(data ?? []), {
          status: 200,
          headers: { "Content-Type": "application/json", ...CORS },
        });
      },
    },
  },
});
