// External engine upserts/deletes the user's current open positions here.
// Token-protected. Accepts either:
//  - { user_id, positions: [...] }  → replaces the snapshot (upsert all, delete missing trade_ids)
//  - { user_id, trade_id, action: "delete" }  → remove a single closed position
import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

const PosSchema = z.object({
  trade_id: z.string().min(1).max(100),
  side: z.string().max(20).optional(),
  setup_name: z.string().max(100).optional(),
  entry_t: z.string().datetime().optional(),
  entry: z.number().optional(),
  sl: z.number().optional(),
  tp: z.number().optional(),
  current_stop: z.number().optional(),
  atr: z.number().optional(),
  bars_held: z.number().int().optional(),
  prob: z.number().optional(),
  threshold: z.number().optional(),
  unrealized_pnl_rate: z.number().nullable().optional(),
});

const Body = z.union([
  z.object({
    user_id: z.string().uuid(),
    positions: z.array(PosSchema).max(50),
  }),
  z.object({
    user_id: z.string().uuid(),
    trade_id: z.string().min(1).max(100),
    action: z.literal("delete"),
  }),
]);

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

export const Route = createFileRoute("/api/public/engine/ingest/open_positions")({
  server: {
    handlers: {
      OPTIONS: async () => new Response(null, { status: 204, headers: CORS }),
      POST: async ({ request }) => {
        const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
        if (!token || token !== process.env.ENGINE_SERVICE_TOKEN)
          return new Response("Unauthorized", { status: 401, headers: CORS });

        let parsed;
        try { parsed = Body.parse(await request.json()); }
        catch (e) {
          return new Response(JSON.stringify({ error: String(e) }), {
            status: 400, headers: { "Content-Type": "application/json", ...CORS },
          });
        }

        const { supabaseAdmin } = await import("@/integrations/supabase/client.server");

        if ("action" in parsed) {
          const { error } = await supabaseAdmin
            .from("open_positions")
            .delete()
            .eq("user_id", parsed.user_id)
            .eq("trade_id", parsed.trade_id);
          if (error) return new Response(JSON.stringify({ error: error.message }), { status: 500, headers: { "Content-Type": "application/json", ...CORS } });
          return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json", ...CORS } });
        }

        const ids = parsed.positions.map((p) => p.trade_id);
        if (parsed.positions.length > 0) {
          const rows = parsed.positions.map((p) => ({ ...p, user_id: parsed.user_id, updated_at: new Date().toISOString() }));
          const { error } = await supabaseAdmin
            .from("open_positions")
            .upsert(rows as never, { onConflict: "user_id,trade_id" });
          if (error) return new Response(JSON.stringify({ error: error.message }), { status: 500, headers: { "Content-Type": "application/json", ...CORS } });
        }
        // Remove any rows no longer in the snapshot
        let del = supabaseAdmin.from("open_positions").delete().eq("user_id", parsed.user_id);
        if (ids.length > 0) del = del.not("trade_id", "in", `(${ids.map((i) => `"${i.replace(/"/g, '""')}"`).join(",")})`);
        const { error: delErr } = await del;
        if (delErr) return new Response(JSON.stringify({ error: delErr.message }), { status: 500, headers: { "Content-Type": "application/json", ...CORS } });

        return new Response(JSON.stringify({ ok: true, count: parsed.positions.length }), {
          status: 200, headers: { "Content-Type": "application/json", ...CORS },
        });
      },
    },
  },
});
