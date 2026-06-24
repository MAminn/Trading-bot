// The engine polls this to know whether to run and to fetch the user's
// Binance keys. Token-protected. Returns decrypted secrets, so this MUST
// only be called server-to-server by the engine.
import { createFileRoute } from "@tanstack/react-router";

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
        if (!token || token !== process.env.ENGINE_SERVICE_TOKEN) return new Response("Unauthorized", { status: 401, headers: CORS });
        const url = new URL(request.url);
        const userId = url.searchParams.get("user_id");
        if (!userId || !/^[0-9a-f-]{36}$/i.test(userId))
          return new Response(JSON.stringify({ error: "missing/invalid user_id" }), { status: 400, headers: { "Content-Type": "application/json", ...CORS } });
        const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
        const { data: cfg, error: cfgErr } = await supabaseAdmin
          .from("engine_config").select("*").eq("user_id", userId).maybeSingle();
        if (cfgErr) return new Response(JSON.stringify({ error: cfgErr.message }), { status: 500, headers: { "Content-Type": "application/json", ...CORS } });
        if (!cfg) return new Response(JSON.stringify({ error: "not found" }), { status: 404, headers: { "Content-Type": "application/json", ...CORS } });

        const { data: keyRow } = await supabaseAdmin
          .from("binance_keys").select("api_key_encrypted, api_secret_encrypted")
          .eq("user_id", userId).maybeSingle();

        let binance: { api_key: string; api_secret: string } | null = null;
        if (keyRow) {
          const { decryptBuffer } = await import("@/lib/crypto.server");
          // Supabase returns bytea as a hex string starting with "\\x" or a Buffer
          const toBuf = (v: unknown): Buffer => {
            if (typeof v === "string") {
              const hex = v.startsWith("\\x") ? v.slice(2) : v;
              return Buffer.from(hex, "hex");
            }
            return Buffer.from(v as ArrayBuffer);
          };
          try {
            binance = {
              api_key: decryptBuffer(toBuf((keyRow as { api_key_encrypted: unknown }).api_key_encrypted)),
              api_secret: decryptBuffer(toBuf((keyRow as { api_secret_encrypted: unknown }).api_secret_encrypted)),
            };
          } catch {
            binance = null;
          }
        }

        return new Response(JSON.stringify({ config: cfg, binance }), {
          status: 200,
          headers: { "Content-Type": "application/json", ...CORS },
        });
      },
    },
  },
});
