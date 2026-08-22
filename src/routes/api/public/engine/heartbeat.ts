import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

// The ML signal worker's liveness. One worker serves every client, so an
// absent user_id mirrors the same heartbeat onto every running client's
// engine_status row — otherwise a newly onboarded client would see "worker
// offline" forever while the worker was in fact running for them.
const Body = z.object({
  // Optional: absent means mirror to every running client.
  user_id: z.string().uuid().optional(),
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

        let targets: string[];
        if (parsed.user_id) {
          targets = [parsed.user_id];
        } else {
          const { loadSignalSubscribers } = await import("@/lib/engine-roster.server");
          try {
            targets = await loadSignalSubscribers();
          } catch (e) {
            return new Response(JSON.stringify({ error: String(e) }), { status: 500, headers: { "Content-Type": "application/json", ...CORS } });
          }
          if (targets.length === 0)
            return new Response(JSON.stringify({ ok: true, delivered: 0 }), { status: 200, headers: { "Content-Type": "application/json", ...CORS } });
        }

        const rows = targets.map((user_id) => ({
          user_id,
          status: parsed.status,
          current_position: parsed.current_position ?? "FLAT",
          message: parsed.message ?? null,
          last_heartbeat: now,
          updated_at: now,
        }));
        const { error } = await supabaseAdmin
          .from("engine_status")
          .upsert(rows as never, { onConflict: "user_id" });
        if (error) return new Response(JSON.stringify({ error: error.message }), { status: 500, headers: { "Content-Type": "application/json", ...CORS } });
        return new Response(JSON.stringify({ ok: true, delivered: rows.length }), { status: 200, headers: { "Content-Type": "application/json", ...CORS } });
      },
    },
  },
});
