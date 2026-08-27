import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";

/** One page. Bounds a single response, not the feed. */
export const MAX_ORDER_PAGE = 1000;

/** Postgres renders timestamptz with an offset, so offsets must be accepted. */
const Timestamp = z.string().datetime({ offset: true });

const Query = z.object({
  user_id: z.string().uuid(),
  symbol: z.string().min(1).max(30).optional(),
  /** Only orders created at or after this are returned. */
  since: Timestamp.optional(),
  limit: z.coerce.number().int().min(1).max(MAX_ORDER_PAGE).default(500),
  /** Keyset cursor: the (created_at, id) of the last row of the previous page. */
  after_created_at: Timestamp.optional(),
  after_id: z.string().uuid().optional(),
  /** Fixed upper bound, established on page 1 and echoed on every page after. */
  snapshot_before: Timestamp.optional(),
});

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
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
 * Accounting-only order feed. Keyset-paginated, and honest about it.
 *
 * The synchroniser uses these rows for ATTRIBUTION: which Binance order ids came
 * from Helix. An incomplete set is not a smaller answer, it is a WRONG one — a
 * Helix OPEN order missing from the list makes the position it opened look like
 * the client's own trading, and a real trade with real commission silently
 * disappears from their P&L.
 *
 * WHY NOT OFFSET. `engine_orders` rows are not append-only. `ingest/order_update`
 * mutates `status` (INTENT_LOGGED -> SENT -> FILLED) and populates
 * `binance_order_id` after the row already exists — and this query filters on
 * exactly those two columns. So a row created an hour ago can ENTER the filtered
 * result set midway through a walk, shifting every later offset by one and
 * making an offset walk skip or duplicate a row. Skipping is the dangerous half:
 * the skipped row could be the OPEN that attributes a real trade.
 *
 * KEYSET instead, on (created_at, id):
 *
 *   created_at > cursor.created_at
 *     OR (created_at = cursor.created_at AND id > cursor.id)
 *   AND created_at <= snapshot_before
 *
 * The cursor names a position in a total ordering rather than a count, so rows
 * appearing behind it cannot move the rows ahead of it. `id` is the tie-breaker
 * for rows sharing a timestamp, which a bare `created_at` cursor would either
 * skip or loop on. `snapshot_before` is fixed on page 1 and reused for the whole
 * walk, so rows created while the walk is running cannot extend it indefinitely.
 *
 * A row that becomes eligible BEHIND the cursor is simply not seen this run, and
 * is picked up on the next one. That is the deliberate trade: a trade briefly
 * absent is safe, a trade wrongly attributed is not.
 *
 * It writes nothing, and is not called by signal generation, sizing, order
 * placement or reconciliation.
 */
export const Route = createFileRoute("/api/public/engine/accounting/orders")({
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
            symbol: url.searchParams.get("symbol") ?? undefined,
            since: url.searchParams.get("since") ?? undefined,
            limit: url.searchParams.get("limit") ?? undefined,
            after_created_at: url.searchParams.get("after_created_at") ?? undefined,
            after_id: url.searchParams.get("after_id") ?? undefined,
            snapshot_before: url.searchParams.get("snapshot_before") ?? undefined,
          });
        } catch (e) {
          return json({ error: String(e) }, 400);
        }

        // Half a cursor is a malformed cursor. Refused rather than interpreted:
        // treating it as "start from the beginning" would silently re-walk and
        // could loop, and treating it as "no cursor" would skip a page.
        if (Boolean(parsed.after_created_at) !== Boolean(parsed.after_id)) {
          return json(
            { error: "after_created_at and after_id must be provided together" },
            400,
          );
        }

        // Established once, on the first page, and echoed back for the caller to
        // pin every subsequent page to.
        const snapshot_before = parsed.snapshot_before ?? new Date().toISOString();

        const { supabaseAdmin } = await import("@/integrations/supabase/client.server");

        // One row beyond the page answers "is there more?" without a second
        // count query that could disagree with this one.
        const probe = parsed.limit + 1;
        let query = supabaseAdmin
          .from("engine_orders")
          .select("id,side,intent,symbol,qty,binance_order_id,status,created_at")
          .eq("user_id", parsed.user_id)
          .in("status", ["SENT", "FILLED"])
          .not("binance_order_id", "is", null)
          .lte("created_at", snapshot_before)
          .order("created_at", { ascending: true })
          .order("id", { ascending: true })
          .limit(probe);
        if (parsed.symbol) query = query.eq("symbol", parsed.symbol);
        if (parsed.since) query = query.gte("created_at", parsed.since);
        if (parsed.after_created_at && parsed.after_id) {
          // The timestamp is double-quoted because a timestamptz contains the
          // same characters PostgREST uses as separators.
          const t = parsed.after_created_at;
          query = query.or(
            `created_at.gt."${t}",and(created_at.eq."${t}",id.gt.${parsed.after_id})`,
          );
        }

        const { data, error } = await query;

        if (error) return json({ error: error.message }, 500);

        const rows = data ?? [];
        const has_more = rows.length > parsed.limit;
        const orders = has_more ? rows.slice(0, parsed.limit) : rows;
        const last = orders[orders.length - 1];

        return json(
          {
            orders,
            count: orders.length,
            // Explicit, so a caller can never mistake a full page for the end.
            has_more,
            // The raw values, never round-tripped through a Date: a timestamptz
            // carries microseconds and JS truncates to milliseconds, which would
            // move the cursor backwards and re-serve rows already returned.
            next_cursor: last ? { created_at: last.created_at, id: last.id } : null,
            snapshot_before,
          },
          200,
        );
      },
    },
  },
});
