// The engine polls this to know how to size orders. Token-protected.
//
// This endpoint returns NO key material. Signing keys live in the executor's
// own environment on the VPS and never traverse the app: returning them here
// would put the account's highest-value secret behind the same shared bearer
// token used by the ingest endpoints, for no capability the executor lacks.
// Only the presence flag and the public key's last 4 chars are exposed, so the
// UI and the executor can report "connected" without handling a secret.
//
// Columns are whitelisted rather than SELECT *: a future column must be added
// here deliberately, and so can never leak by simply existing.
import { createFileRoute } from "@tanstack/react-router";

const CONFIG_FIELDS = [
  "mode",
  "sizing_mode",
  "capital_usd",
  "account_size_usd",
  "capital_allocation_pct",
  "leverage",
  "max_notional_usd",
  "max_daily_loss_usd",
  "max_position_size_usd",
  "is_running",
  "demo_mode",
  "updated_at",
] as const;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

export const Route = createFileRoute("/api/public/engine/config")({
  server: {
    handlers: {
      OPTIONS: async () => new Response(null, { status: 204, headers: CORS }),
      GET: async ({ request }) => {
        const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
        if (!token || token !== process.env.ENGINE_SERVICE_TOKEN)
          return new Response("Unauthorized", { status: 401, headers: CORS });
        const url = new URL(request.url);
        const userId = url.searchParams.get("user_id");
        if (!userId || !/^[0-9a-f-]{36}$/i.test(userId))
          return new Response(JSON.stringify({ error: "missing/invalid user_id" }), {
            status: 400,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
        const { data: cfg, error: cfgErr } = await supabaseAdmin
          .from("engine_config")
          .select(CONFIG_FIELDS.join(","))
          .eq("user_id", userId)
          .maybeSingle();
        if (cfgErr)
          return new Response(JSON.stringify({ error: cfgErr.message }), {
            status: 500,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        if (!cfg)
          return new Response(JSON.stringify({ error: "not found" }), {
            status: 404,
            headers: { "Content-Type": "application/json", ...CORS },
          });

        // Metadata only. The encrypted blobs are never read, let alone decrypted.
        const { data: keyRow } = await supabaseAdmin
          .from("binance_keys")
          .select("api_key_last4")
          .eq("user_id", userId)
          .maybeSingle();
        const last4 = (keyRow as { api_key_last4: string } | null)?.api_key_last4 ?? null;

        return new Response(
          JSON.stringify({ config: cfg, keys_present: last4 !== null, api_key_last4: last4 }),
          {
            status: 200,
            headers: { "Content-Type": "application/json", ...CORS },
          },
        );
      },
    },
  },
});
