import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";
import type { Database } from "@/integrations/supabase/types";

type ExecutedTradeInsert = Database["public"]["Tables"]["executed_trades"]["Insert"];

/**
 * Money arrives as a DECIMAL STRING, not a JSON number.
 *
 * The synchroniser holds every Binance figure as a Python `Decimal` and the
 * column is `numeric`; a JSON number in between would be the one float64 in an
 * otherwise exact path. Postgres parses these strings straight into `numeric`.
 */
const Decimal = z
  .string()
  .trim()
  .regex(/^-?(\d+)(\.\d+)?$/, "expected a plain decimal string");

const NonNegativeDecimal = Decimal.refine((s) => !s.startsWith("-"), "must not be negative");

const Base = z.object({
  user_id: z.string().uuid(),
  symbol: z.string().min(1).max(30).default("ETHUSDT"),
  side: z.enum(["LONG", "SHORT"]),
  open_binance_order_id: z.string().min(1).max(100),
  close_binance_order_id: z.string().min(1).max(100),
  entry_time: z.string().datetime(),
  exit_time: z.string().datetime(),
  entry_fill_count: z.number().int().nonnegative(),
  exit_fill_count: z.number().int().nonnegative(),
  exit_order_count: z.number().int().nonnegative().default(0),
  funding_event_count: z.number().int().nonnegative().default(0),
  // Helix opened every position recorded here; this says how it was CLOSED.
  // A client closing a bot position by hand in the Binance app is a real
  // completed trade with real fees, and the customer is told which route it took.
  close_source: z.enum(["HELIX", "EXTERNAL", "MIXED"]).default("HELIX"),
});

/** Everything Binance reported, in full. */
const Complete = Base.extend({
  accounting_status: z.literal("COMPLETE"),
  qty: NonNegativeDecimal,
  entry_avg_price: NonNegativeDecimal,
  exit_avg_price: NonNegativeDecimal,
  gross_pnl_usd: Decimal,
  entry_commission_usd: NonNegativeDecimal,
  exit_commission_usd: NonNegativeDecimal,
  funding_usd: Decimal,
});

/**
 * A trade we could see but could NOT price exactly — a commission charged in a
 * non-USDT asset, fills missing from the window, entry and exit quantities that
 * do not agree. The row is recorded so the customer is told the accounting is
 * incomplete, and every money column stays NULL so nothing can be read as a
 * figure we stand behind. Money fields are rejected outright here: the only way
 * to reach the database with a value is through the COMPLETE branch.
 */
const Incomplete = Base.extend({
  accounting_status: z.literal("INCOMPLETE"),
  incomplete_reason: z.string().min(1).max(300),
});

const Body = z.discriminatedUnion("accounting_status", [Complete, Incomplete]);

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

function unauthorized() {
  return new Response("Unauthorized", { status: 401, headers: CORS });
}

function json(body: unknown, status: number) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

/**
 * Writes reporting facts only.
 *
 * `commission_usd` and `net_pnl_usd` are GENERATED columns — they are absent
 * from the Insert type above and from every payload accepted here, so no caller
 * can make the total a customer reads disagree with the parts it is made of.
 *
 * The upsert targets the (user_id, close_binance_order_id) unique constraint:
 * re-running the synchroniser restates a closed trade, it never duplicates one.
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
          return json({ error: String(e) }, 400);
        }

        const { supabaseAdmin } = await import("@/integrations/supabase/client.server");

        // The decimal strings above are handed to Postgres verbatim. The
        // generated column type declares `number` because that is what a SELECT
        // returns; on the way in, `numeric` accepts the exact text. This cast is
        // the whole of that mismatch and is deliberately kept to one line.
        const row = {
          ...parsed,
          source: "BINANCE",
          synced_at: new Date().toISOString(),
        } as unknown as ExecutedTradeInsert;

        const { data, error } = await supabaseAdmin
          .from("executed_trades")
          .upsert(row, { onConflict: "user_id,close_binance_order_id" })
          .select(
            "id,accounting_status,incomplete_reason,gross_pnl_usd,entry_commission_usd," +
              "exit_commission_usd,commission_usd,funding_usd,net_pnl_usd",
          )
          .single();

        if (error) return json({ error: error.message }, 500);
        return json(data, 200);
      },
    },
  },
});
