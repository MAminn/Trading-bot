import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

const Body = z.object({
  user_id: z.string().uuid(),
  symbol: z.string().min(1).max(30).default("ETHUSDT"),
  side: z.enum(["LONG", "SHORT"]),
  open_binance_order_id: z.string().min(1).max(100),
  close_binance_order_id: z.string().min(1).max(100),
  entry_time: z.string().datetime(),
  exit_time: z.string().datetime(),
  qty: z.number().positive(),
  entry_avg_price: z.number().positive(),
  exit_avg_price: z.number().positive(),
  entry_fill_count: z.number().int().nonnegative(),
  exit_fill_count: z.number().int().nonnegative(),
  gross_pnl_usd: z.number(),
  entry_commission_usd: z.number().nonnegative(),
  exit_commission_usd: z.number().nonnegative(),
  funding_usd: z.number(),
});

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

function unauthorized() {
  return new Response("Unauthorized", { status: 401, headers: CORS });
}

/**
 * Writes reporting facts only. The generated commission_usd/net_pnl_usd
 * columns are calculated by PostgreSQL so callers cannot make the displayed
 * arithmetic disagree with its components.
 */
export const Route = createFileRoute("/api/public/engine/accounting/trade")({
  server: {
    handlers: {
      OPTIONS: async () => new Response(null, { status: 204, headers: CORS }),
      POST: async ({ request }) => {
        const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
        if (!token || token !== process.env.ENGINE_SERVICE_TOKEN) return unauthorized();

        let parsed;
        try {
          parsed = Body.parse(await request.json());
        } catch (e) {
          return new Response(JSON.stringify({ error: String(e) }), {
            status: 400,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        }

        const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
        const row = {
          ...parsed,
          source: "BINANCE",
          synced_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };

        // executed_trades is introduced by the same deployment as this route;
        // generated Supabase client types can lag one migration. Keep that
        // compile-time concern local to this reporting-only boundary.
        const db = supabaseAdmin as any;
        const { data, error } = await db
          .from("executed_trades")
          .upsert(row, { onConflict: "user_id,close_binance_order_id" })
          .select("id,commission_usd,net_pnl_usd")
          .single();

        if (error) {
          return new Response(JSON.stringify({ error: error.message }), {
            status: 500,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        }

        return new Response(JSON.stringify(data), {
          status: 200,
          headers: { "Content-Type": "application/json", ...CORS },
        });
      },
    },
  },
});
