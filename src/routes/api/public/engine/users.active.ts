// The multi-tenant executor asks here which clients it should be running.
//
// This is what removes the operator from onboarding. Previously the executor
// took a single ENGINE_USER_ID from its environment, so a new client could sign
// up, connect their keys and press Start while nothing happened until someone
// edited a `.env` and restarted a container. Now the executor polls this and
// picks the client up on the next cycle.
//
// The roster grants ATTENTION, never CAPABILITY. Appearing here only means the
// executor will build a session and start reporting telemetry for that user.
// Whether they may actually place an order is decided per cycle, per user, by
// the executor: the host's .env ceiling, the user's own execution_mode,
// auto_execute, is_running, live cap, LIVE_TRADING_ACK, and whether their keys
// resolve. Nothing here can raise any of those.
//
// No key material. The response is user ids and nothing else — deliberately not
// even a boolean saying whether an account holds credentials, so this endpoint
// cannot become a way to enumerate which clients have connected a wallet.
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

export const Route = createFileRoute("/api/public/engine/users/active")({
  server: {
    handlers: {
      OPTIONS: async () => new Response(null, { status: 204, headers: CORS }),
      GET: async ({ request }) => {
        const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
        if (!token || token !== process.env.ENGINE_SERVICE_TOKEN)
          return new Response("Unauthorized", { status: 401, headers: CORS });

        const { loadExecutionRoster, MAX_ROSTER_USERS } =
          await import("@/lib/engine-roster.server");

        let roster;
        try {
          roster = await loadExecutionRoster();
        } catch (e) {
          // The executor treats a failed roster fetch as "keep the sessions I
          // already have" rather than "stop trading for everyone", so a 500
          // here is survivable by design.
          return json({ error: String(e) }, 500);
        }

        return json(
          {
            users: roster.userIds.map((user_id) => ({ user_id })),
            count: roster.userIds.length,
            // Surfaced rather than silently applied: a truncated roster means
            // some client is not being executed, which an operator must know.
            truncated: roster.truncated,
            max: MAX_ROSTER_USERS,
          },
          200,
        );
      },
    },
  },
});
