import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

const Body = z.object({
  user_id: z.string().uuid(),
  bar_time: z.string().datetime(),
  bar_closed_now: z.boolean().optional(),
  valid_next_entry: z.boolean().optional(),
  rule_side: z.number().int().min(-1).max(1).optional(),
  rule_reason: z.string().max(500).optional(),
  ml_prob: z.number().optional(),
  ml_threshold: z.number().optional(),
  ml_accept: z.boolean().optional(),
  opened: z.string().max(50).optional(),
  closed_reason: z.string().max(200).optional(),
  position_before: z.string().max(20).optional(),
  position_after: z.string().max(20).optional(),
  trade_id: z.string().max(100).optional(),
});

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

function unauthorized() {
  return new Response("Unauthorized", { status: 401, headers: CORS });
}

export const Route = createFileRoute("/api/public/engine/ingest/signal")({
  server: {
    handlers: {
      OPTIONS: async () => new Response(null, { status: 204, headers: CORS }),
      POST: async ({ request }) => {
        const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
        if (!token || token !== process.env.ENGINE_SERVICE_TOKEN) return unauthorized();
        let parsed;
        try { parsed = Body.parse(await request.json()); }
        catch (e) { return new Response(JSON.stringify({ error: String(e) }), { status: 400, headers: { "Content-Type": "application/json", ...CORS } }); }
        const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
        const { error } = await supabaseAdmin.from("user_signals").insert(parsed as never);
        if (error) return new Response(JSON.stringify({ error: error.message }), { status: 500, headers: { "Content-Type": "application/json", ...CORS } });
        return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json", ...CORS } });
      },
    },
  },
});
