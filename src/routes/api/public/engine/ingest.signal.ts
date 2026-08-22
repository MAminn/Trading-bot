// The ML worker posts each strategy signal here.
//
// A signal is a property of the MARKET, not of a user: live_code.py runs one
// frozen ETHUSDT strategy, and the signal at a given bar is identical for
// everyone subscribed to it. So the worker may post ONE signal with no user_id
// and the app copies it to every running client — which is what lets a client
// who signed up ten minutes ago start receiving signals with no operator
// action and no per-client worker process.
//
// Posting WITH a user_id is still supported and unchanged: that is the
// single-tenant path, and a worker pinned to ENGINE_USER_ID keeps working
// exactly as it did.
import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

const Body = z.object({
  // Optional: absent means broadcast to every running client.
  user_id: z.string().uuid().optional(),
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

        const { user_id: addressed, ...signal } = parsed;

        // Single-tenant path, untouched.
        if (addressed) {
          const { error } = await supabaseAdmin
            .from("user_signals")
            .insert({ ...signal, user_id: addressed } as never);
          if (error) return new Response(JSON.stringify({ error: error.message }), { status: 500, headers: { "Content-Type": "application/json", ...CORS } });
          return new Response(JSON.stringify({ ok: true, delivered: 1 }), { status: 200, headers: { "Content-Type": "application/json", ...CORS } });
        }

        // Broadcast path.
        const { loadSignalSubscribers } = await import("@/lib/engine-roster.server");
        let subscribers: string[];
        try {
          subscribers = await loadSignalSubscribers();
        } catch (e) {
          return new Response(JSON.stringify({ error: String(e) }), { status: 500, headers: { "Content-Type": "application/json", ...CORS } });
        }
        if (subscribers.length === 0)
          return new Response(JSON.stringify({ ok: true, delivered: 0 }), { status: 200, headers: { "Content-Type": "application/json", ...CORS } });

        // Idempotency. The worker retries on a timeout, and a broadcast retry
        // would otherwise deliver a second copy of the same bar to everyone —
        // which the executor would read as a second signal and could act on.
        // Users who already hold this bar are excluded rather than upserted, so
        // a redelivery is a no-op instead of a rewrite.
        const { data: existing, error: existingErr } = await supabaseAdmin
          .from("user_signals")
          .select("user_id")
          .eq("bar_time", signal.bar_time)
          .in("user_id", subscribers);
        if (existingErr) return new Response(JSON.stringify({ error: existingErr.message }), { status: 500, headers: { "Content-Type": "application/json", ...CORS } });

        const already = new Set((existing ?? []).map((r) => (r as { user_id: string }).user_id));
        const targets = subscribers.filter((id) => !already.has(id));
        if (targets.length === 0)
          return new Response(JSON.stringify({ ok: true, delivered: 0, duplicate: true }), { status: 200, headers: { "Content-Type": "application/json", ...CORS } });

        const rows = targets.map((user_id) => ({ ...signal, user_id }));
        const { error } = await supabaseAdmin.from("user_signals").insert(rows as never);
        if (error) return new Response(JSON.stringify({ error: error.message }), { status: 500, headers: { "Content-Type": "application/json", ...CORS } });
        return new Response(JSON.stringify({ ok: true, delivered: rows.length }), { status: 200, headers: { "Content-Type": "application/json", ...CORS } });
      },
    },
  },
});
