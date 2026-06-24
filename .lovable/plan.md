## Goal

Connect the existing Helix UI (landing, `/auth`, `/app/dashboard`, `/app/engine`, etc.) to a real per-user backend. The Python trading engine runs externally — this app only stores, displays, and controls. No Python, no ML, no simulated signals inside the app.

The existing schema is global (single-row `system_status`, no `user_id` on `signals` / `trades`). I'll add the new per-user tables alongside it and migrate the dashboard to read from the new ones. The old global tables stay (used by the worker scaffold under `/worker/`) but the UI will no longer read from them.

## 1. Database (migration, RLS on, per-user)

New tables in `public`, each with `GRANT` + RLS scoped to `auth.uid() = user_id`:

- **`engine_config`** — `user_id` (unique), `mode` ('signal_only'|'auto', default 'signal_only'), `capital_usd`, `leverage`, `max_daily_loss_usd`, `max_position_size_usd`, `is_running` (bool), `updated_at`. Auto-insert default row on signup via trigger.
- **`binance_keys`** — `user_id` (unique), `api_key_encrypted`, `api_secret_encrypted`, `api_key_last4`, `permissions_note`, `created_at`. RLS lets user `SELECT` only non-secret columns via a view; secret never returned to browser.
- **`user_signals`** — `user_id`, `bar_time`, `bar_closed_now`, `valid_next_entry`, `rule_side`, `rule_reason`, `ml_prob`, `ml_threshold`, `ml_accept`, `opened`, `closed_reason`, `position_before`, `position_after`, `created_at`. User can `SELECT` own rows; only service role inserts.
- **`user_trades`** — full schema as specified. Same RLS shape.
- **`engine_status`** — `user_id` (unique), `status` ('running'|'stopped'|'error'), `last_heartbeat`, `current_position` ('FLAT'|'LONG'|'SHORT'), `message`, `updated_at`. Same RLS shape.

Realtime: `ALTER PUBLICATION supabase_realtime ADD TABLE` for `engine_status`, `user_signals`, `user_trades`, `engine_config`.

Encryption: use `pgcrypto` with a server-only secret (`BINANCE_KEY_ENCRYPTION_SECRET`) via SECURITY DEFINER function `encrypt_binance_keys(user_id, api_key, api_secret)` so the secret never reaches the client.

## 2. Edge contract with the Python engine

Public TanStack server routes under `src/routes/api/public/engine/` guarded by a shared `ENGINE_SERVICE_TOKEN` header (Bearer). Each loads `supabaseAdmin` inside the handler.

- `POST /api/public/engine/ingest/signal` → insert `user_signals` row
- `POST /api/public/engine/ingest/trade` → insert `user_trades` row
- `POST /api/public/engine/heartbeat` → upsert `engine_status`
- `GET  /api/public/engine/config?user_id=...` → return `engine_config` + decrypted Binance keys
- `GET  /api/public/engine/eth-price` (public, no token) → proxy `https://api.binance.com/api/v3/ticker/24hr?symbol=ETHUSDT` for the ticker (avoids CORS)

Zod-validate all bodies. Reject without a valid token.

## 3. Server functions (app-internal)

In `src/lib/`:

- `engine.functions.ts` — `getMyEngineConfig`, `setEngineRunning(bool)`, `updateEngineConfig(partial)`, `getMyEngineStatus`. All `requireSupabaseAuth`.
- `binance.functions.ts` — `saveBinanceKeys({apiKey, apiSecret})` (calls the SECURITY DEFINER encrypt fn), `getBinanceKeyInfo` (returns last4 + created_at only), `deleteBinanceKeys`.

## 4. Wire the dashboard

Replace the current `useSystemStatus / useSignals / useTrades` (which read global tables) with per-user hooks in a new `src/lib/user-engine.ts`:

- Status pill: green if `status='running'` AND `last_heartbeat` within 30 min; amber "Stale" if running but heartbeat older; red on `error`; grey otherwise.
- KPIs: compute from `user_trades` (`net_pnl_rate`). Equity = `capital_usd + Σ(net_pnl_rate * capital_usd)` chronologically.
- Current signal: latest `user_signals` row.
- Trade history + recent signals: realtime tables.
- ETH ticker: poll `/api/public/engine/eth-price` every 10s (replaces existing `live-price.ts`).

Subscribe via Supabase realtime; teardown on unmount.

## 5. Controls

- Big Start/Stop in dashboard + `/app/engine` → `setEngineRunning`.
- `/app/engine`: status detail (status/heartbeat/position/message), copy-to-clipboard ingest URLs + the `ENGINE_SERVICE_TOKEN` (admin only — gated by `is_admin()`), current active `engine_config` values.
- `/app/connect`: Binance form → `saveBinanceKeys`; show only `••••last4`; trade-only/no-withdrawals warning.
- `/app/settings` / `/app/configure`: edit `engine_config` (capital, leverage, max daily loss, max position size, mode). Mode `auto` disabled + "coming soon".

## 6. Safety

- `engine_config.mode` CHECK constraint + UI lock to `signal_only`.
- Binance secret never selectable by `authenticated`; only the SECURITY DEFINER encrypt fn writes it; only edge function (service role + service token) reads it via `/engine/config`.
- Start/Stop and live status remain the visual focus on the dashboard.

## Secrets needed

- `ENGINE_SERVICE_TOKEN` — shared with the Python engine, validated by `/api/public/engine/*`.
- `BINANCE_KEY_ENCRYPTION_SECRET` — used by `pgcrypto` `encrypt`/`decrypt` SECURITY DEFINER functions.

I'll request both via the secrets tool after you approve this plan.

## Build order

1. Migration (tables, RLS, GRANTs, encrypt fns, realtime publication, signup trigger for default `engine_config` + `engine_status`).
2. Request secrets.
3. Server routes under `/api/public/engine/*` + server functions.
4. New `src/lib/user-engine.ts` hooks; rewrite `app.dashboard.tsx` bindings.
5. `/app/engine`, `/app/connect`, `/app/settings` rewires.
6. ETH ticker via edge route; remove `live-price.ts` direct Binance call.

## Technical notes

- Keep `worker/`, old global tables, and the worker scaffold intact — out of scope, no UI reads from them after this change.
- `supabaseAdmin` only inside handler bodies (per project rules).
- All new public-schema tables get `GRANT SELECT, INSERT, UPDATE, DELETE ... TO authenticated` + `GRANT ALL ... TO service_role`. No `anon` grants.
- `auto` mode rejected at the DB layer via CHECK until Phase 2.

Ready to proceed once you approve — I'll start with the migration.
