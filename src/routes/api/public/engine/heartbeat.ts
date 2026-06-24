import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

const Body = z.object({
  user_id: z.string().uuid(),
  status: z.enum(["running", "stopped", "error"]),
  current_position: z.enum(["FLAT", "LONG", "SHORT"]).optional(),
  message: z.string().max(500).optional(),
});

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

export const Route = createFileRoute("/api/public/engine/heartbeat")({
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
        const now = new Date().toISOString();
        const { error } = await supabaseAdmin
          .from("engine_status")
          .upsert({
            user_id: parsed.user_id,
            status: parsed.status,
            current_position: parsed.current_position ?? "FLAT",
            message: parsed.message ?? null,
            last_heartbeat: now,
            updated_at: now,
          } as never, { onConflict: "user_id" });
        if (error) return new Response(JSON.stringify({ error: error.message }), { status: 500, headers: { "Content-Type": "application/json", ...CORS } });
        return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json", ...CORS } });
      },
    },
  },
});
