// The read-only accounting process asks here whose Binance history it should
// synchronise.
//
// Separate from /api/public/engine/users/active on purpose. That endpoint is
// the EXECUTION roster, gated on `execution_mode IN (LIVE_READ, LIVE_TRADE)`,
// which is the right rule for deciding who to trade for and the wrong one for
// deciding whose money to account for. A customer who presses Stop, switches
// execution off, or disables live trading still made the trades they already
// made: the commission was charged, the P&L was realised, and it must keep
// reaching their dashboard. Reusing the execution roster would erase a client's
// financial history the moment they stopped trading.
//
// Eligibility here is credential ownership and nothing else — not is_running,
// not execution_mode, not demo_mode. If there are Binance keys on file there is
// an account to read; if there are not, there is no history to fetch.
//
// No key material. The response is user ids and a truncation flag. The
// underlying query selects `user_id` alone, so nothing about a customer's keys
// beyond "a row exists" can leave this endpoint — not last4, not the encrypted
// blobs, and no decrypt is attempted.
import { createFileRoute } from "@tanstack/react-router";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

function json(body: unknown, status: number) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

export const Route = createFileRoute("/api/public/engine/accounting/users")({
  server: {
    handlers: {
      OPTIONS: async () => new Response(null, { status: 204, headers: CORS }),
      GET: async ({ request }) => {
        const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
        if (!token || token !== process.env.ENGINE_SERVICE_TOKEN)
          return new Response("Unauthorized", { status: 401, headers: CORS });

        const { loadAccountingRoster, MAX_ACCOUNTING_USERS } =
          await import("@/lib/accounting-roster.server");

        let roster;
        try {
          roster = await loadAccountingRoster();
        } catch (e) {
          // A 500, never an empty list. The accounting process treats a failed
          // roster as "do not know yet" and writes nothing; an empty 200 would
          // read as "no customers have keys" and be indistinguishable from a
          // database outage.
          return json({ error: String(e) }, 500);
        }

        return json(
          {
            users: roster.userIds.map((user_id) => ({ user_id })),
            count: roster.userIds.length,
            // Surfaced rather than silently applied: a truncated roster means
            // some customer's real P&L is not being synchronised.
            truncated: roster.truncated,
            max: MAX_ACCOUNTING_USERS,
          },
          200,
        );
      },
    },
  },
});
