# Binance key handling

How a client's Binance credentials get from the website to a signed mainnet
order, and every place they are deliberately not allowed to go.

## The problem this replaced

A client could sign up, enter their Binance API key and secret at `/app/connect`,
and see the account reported as connected — while `executor/main.py` selected
its mainnet credentials purely from the process environment:

```python
"LIVE_READ":  ("BINANCE_LIVE_API_KEY", "BINANCE_LIVE_API_SECRET"),
"LIVE_TRADE": ("BINANCE_LIVE_API_KEY", "BINANCE_LIVE_API_SECRET"),
```

Every live order therefore moved the **server operator's** funds, no matter who
was logged in. The client's stored keys were written, encrypted, and never read
by anything. This was not a misconfiguration; there was no configuration that
would have made it behave correctly.

## The path now

```
browser ──► saveBinanceKeys()  ──► AES-256-GCM ──► supabase.binance_keys
             (server function)     crypto.server.ts   (bytea, RLS on)

executor ──► GET /api/public/engine/credentials?user_id=…
              Authorization: Bearer ENGINE_CREDENTIALS_TOKEN
         ◄── { api_key, api_secret, api_key_last4 }
         └─► BinanceFuturesClient(LIVE_BASE_URL, api_key, api_secret)
```

`ENGINE_USER_ID` is optional. Left empty (the production setting) the executor
runs a session per active client, each fetching its own user id's keys. Set, it
pins the process to one client. Either way a session can only ever fetch the
keys of the user it was constructed for — see ONBOARDING_ARCHITECTURE.md.

### Where decryption happens

Exactly one place: `loadUserBinanceCredentials()` in
`src/lib/binance-credentials.server.ts`, called only from the credentials route,
only server-side. `src/lib/binance-credentials.test.ts` asserts that no other
route imports it and that no `.tsx` file imports it or `crypto.server`.

### Why the executor asks the app rather than decrypting itself

Decrypting in the executor would require both the Supabase service-role key and
`BINANCE_KEY_ENCRYPTION_SECRET` on the VPS. That secret is the master key for
**every** user's stored credentials, so a compromise of the trading host would
expose all of them. As built, a compromised host yields only the keys of the one
user it was already trading for — which it necessarily holds anyway.

### Why a second token

`ENGINE_SERVICE_TOKEN` is accepted by the config, signal, ingest and telemetry
routes. Serving key material behind it would extend a dozen endpoints' worth of
exposure to the account's highest-value secret. `/api/public/engine/credentials`
therefore takes its own `ENGINE_CREDENTIALS_TOKEN`, and both sides refuse to
operate if the two are equal: the app returns 503, the executor returns exit 1.

The credentials route also sends **no CORS headers** — every sibling route
answers `*` because a browser calls it; nothing in a browser may call this one —
and responds `Cache-Control: no-store`.

## Fail-closed behaviour

A live mode with no usable client keys does not fall back and does not exit. It
builds no client at all, makes no signed call, and keeps heartbeating so the
Engine page shows the reason:

| Situation | `blocked_reason` | `orders_enabled` | `keys_present` |
| --- | --- | --- | --- |
| Client has not connected keys | `missing_user_binance_keys` | `false` | `false` |
| Row exists, cannot be decrypted | `user_binance_keys_undecryptable` | `false` | `false` |
| Endpoint missing its own token | `credentials_endpoint_not_configured` | `false` | `false` |
| App unreachable | cycle fails and retries | `false` | unchanged |

The distinction matters operationally: the first is fixed by the client pressing
Connect, the second and third by an operator, the fourth by nobody.

Credentials are re-resolved **every cycle**, so connecting, rotating or
disconnecting keys on the website takes effect within one cycle rather than at
the next restart. A rotation rebuilds the client and re-runs clock sync, symbol
filters, the bracket probe and leverage enforcement before anything is sized.

## What is never exposed

- `/api/public/engine/config` returns `keys_present` and `api_key_last4` only.
  It selects no encrypted column and imports nothing that can decrypt.
- The `executor_status` telemetry schema has no key field. `keys_present` is a
  boolean.
- The Engine page shows `····<last4>`, read through `get_my_binance_key_info()`,
  which is RLS-scoped to the signed-in user.
- `UserCredentials.__repr__` renders `<UserCredentials last4='1234'>`, so the
  pair cannot reach a log line, an f-string or a traceback frame.
- The credentials endpoint's failure vocabulary is three fixed strings; no
  cipher error text is ever routed into a response.

## Legacy items — status

### Server-wide live keys: removed from the live path

`BINANCE_LIVE_API_KEY` / `BINANCE_LIVE_API_SECRET` are **not a fallback**. No
live code path reads them; `MODE_CREDENTIAL_ENV` contains testnet entries only,
so there is no expression left that turns an environment variable into a mainnet
signing key. If they are still set, the executor logs a warning that they are
being ignored.

**Action:** delete both from the VPS `.env`, then revoke them on Binance once
the first real client run is verified.

`BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` are unchanged — they
are throwaway host credentials and still come from the environment.

### pgcrypto key functions: dropped

`supabase/migrations/20260822120000_drop_legacy_pgcrypto_binance_key_functions.sql`
removes `save_binance_keys(text,text,text)` and `decrypt_binance_keys_for(uuid)`.

They encrypted with `pgp_sym_encrypt` and a Postgres GUC passphrase — a format
the current AES-256-GCM code cannot read, and vice versa. `save_binance_keys`
was granted to `authenticated`, so any signed-in user could call it over
PostgREST and overwrite their own row in the unreadable format. Nothing would
error, `api_key_last4` would still look right, and their live trading would
simply stop as `user_binance_keys_undecryptable`.

Recovery for a row in that state: the user re-enters their keys at
`/app/connect`, which rewrites it in the current format.

## Rotating `BINANCE_KEY_ENCRYPTION_SECRET`

There is no re-encryption path. Changing it makes every stored row unreadable;
every affected client must re-enter their keys. Executors report
`user_binance_keys_undecryptable` and place no orders in the meantime — they do
not trade with stale credentials.

## Tests

| File | Covers |
| --- | --- |
| `src/lib/binance-credentials.test.ts` | Config/telemetry expose no key material; one decrypting route; CORS/cache/method shape; token separation; round-trip, legacy-format and tampered-blob handling |
| `executor/test_user_credentials.py` | Live modes sign with the user's keys; `.env` live keys unreachable; fail-closed with no keys; rotation and mid-run disconnection; existing live gates intact; no key material in telemetry or logs |
| `executor/test_live_modes.py` | No live mode can source credentials from the environment |

Neither suite touches a network, and no test can create an order.
