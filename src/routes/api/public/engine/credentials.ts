// The executor fetches the connected user's Binance credentials here, so that
// LIVE trading signs with the CLIENT's keys rather than whatever happened to be
// in the server's .env.
//
// This is the one endpoint in the app that returns key material, and it is
// built to be the only one:
//
//  * It is authenticated by ENGINE_CREDENTIALS_TOKEN, which is deliberately
//    NOT the ENGINE_SERVICE_TOKEN shared with the ingest and config endpoints.
//    config.ts declined to serve keys precisely because doing so would put the
//    account's highest-value secret behind a token that a dozen other routes
//    already accept; a distinct token restores that separation instead of
//    arguing the point away. If the token is unset, or set to the same value as
//    the service token, the endpoint refuses to serve anything at all.
//  * It sends NO CORS headers. Every sibling route here answers `*` because a
//    browser legitimately calls it. Nothing in a browser may call this one, so
//    a preflight simply fails and an accidental fetch() from the app is blocked
//    by the browser itself.
//  * It is no-store. No proxy, CDN or browser cache may retain the response.
//  * It never logs, echoes or errors with key material. The failure vocabulary
//    is three fixed strings, and `missing_user_binance_keys` is the exact
//    blocked_reason the executor reports upward.
//
// Decryption happens here and only here, in the server's trusted context, using
// the same AES-256-GCM mechanism that saveBinanceKeys() wrote with.
import { createFileRoute } from "@tanstack/react-router";

const JSON_HEADERS = {
  "Content-Type": "application/json",
  // Key material must not survive the response it was sent in.
  "Cache-Control": "no-store, no-cache, must-revalidate, private",
  Pragma: "no-cache",
  // Belt and braces on a route that should never be reachable from a page.
  "Referrer-Policy": "no-referrer",
};

function json(body: unknown, status: number) {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

export const Route = createFileRoute("/api/public/engine/credentials")({
  server: {
    handlers: {
      GET: async ({ request }) => {
        const { loadUserBinanceCredentials, resolveCredentialsToken } =
          await import("@/lib/binance-credentials.server");

        const expected = resolveCredentialsToken(process.env);
        if (expected === null)
          // Deliberately indistinguishable from a bad token to an unauthorised
          // caller, but distinct in the body so an operator reading the
          // executor's log knows to set the variable.
          return json({ error: "credentials_endpoint_not_configured" }, 503);

        const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
        if (!token || token !== expected) return json({ error: "unauthorized" }, 401);

        const url = new URL(request.url);
        const userId = url.searchParams.get("user_id");
        if (!userId || !/^[0-9a-f-]{36}$/i.test(userId))
          return json({ error: "missing/invalid user_id" }, 400);

        const result = await loadUserBinanceCredentials(userId);

        if (result.status === "missing")
          // The executor turns this exact string into its blocked_reason.
          return json({ error: "missing_user_binance_keys" }, 404);
        if (result.status === "undecryptable")
          // A row exists but this server cannot read it: a wrong or rotated
          // BINANCE_KEY_ENCRYPTION_SECRET, or a legacy pgcrypto row. Reported
          // as its own state so it is never mistaken for "user has no keys",
          // which would send an operator hunting in the wrong place.
          return json({ error: "credentials_undecryptable" }, 409);

        return json(
          {
            api_key: result.apiKey,
            api_secret: result.apiSecret,
            api_key_last4: result.last4,
          },
          200,
        );
      },
    },
  },
});
