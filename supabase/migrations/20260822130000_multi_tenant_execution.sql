-- Multi-tenant execution: indexes for the roster and the signal fan-out.
--
-- NO new columns, no new tables, no changed grants, no changed RLS. Everything
-- dynamic onboarding needs already existed per user:
--
--   engine_config     one row per user, created by the on_auth_user_created_engine
--                     trigger at signup. Carries execution_mode, is_running,
--                     mode/auto_execute_enabled, live_order_cap_usd,
--                     live_allow_full_capital, demo_mode.
--   binance_keys      one row per user, written encrypted at /app/connect.
--   executor_status   one row per user, written by that user's session.
--   user_signals /
--   user_trades /
--   engine_orders     already user_id-scoped with owner-only RLS.
--
-- So a client who signs up today is already fully represented in the schema.
-- What was missing was an executor willing to look at them.

-- --------------------------------------------------------------------------
-- 1. The execution roster query.
-- --------------------------------------------------------------------------
-- /api/public/engine/users/active runs
--   SELECT user_id, execution_mode, is_running, demo_mode
--     FROM engine_config
--    WHERE execution_mode IN ('LIVE_READ','LIVE_TRADE')
-- on every executor loop iteration, i.e. once a minute forever. Partial, because
-- the overwhelming majority of rows are OFF and never need to be visited.
CREATE INDEX IF NOT EXISTS engine_config_execution_roster_idx
  ON public.engine_config (execution_mode)
  WHERE execution_mode <> 'OFF';

-- --------------------------------------------------------------------------
-- 2. The signal fan-out target query.
-- --------------------------------------------------------------------------
-- A broadcast signal is copied to every client with is_running = true. This runs
-- once per signal bar, and again per trade and per heartbeat.
CREATE INDEX IF NOT EXISTS engine_config_running_idx
  ON public.engine_config (is_running)
  WHERE is_running;

-- --------------------------------------------------------------------------
-- 3. Fan-out idempotency lookup.
-- --------------------------------------------------------------------------
-- The worker retries on a timeout. Before inserting, the route asks which of the
-- target users already hold a row for this bar_time, so a redelivery is a no-op
-- rather than a second copy of the same signal — which the executor would read
-- as a second signal and could act on.
--
-- (user_id, bar_time) rather than (bar_time, user_id): the existing
-- user_signals_user_time_idx is on (user_id, created_at DESC) and does not serve
-- a bar_time lookup, and user_id-leading keeps this useful for per-user reads
-- too.
CREATE INDEX IF NOT EXISTS user_signals_user_bar_idx
  ON public.user_signals (user_id, bar_time);

-- --------------------------------------------------------------------------
-- NOT DONE HERE, ON PURPOSE
-- --------------------------------------------------------------------------
-- A UNIQUE index on user_signals(user_id, bar_time) would make fan-out
-- idempotency a database guarantee rather than an application one, which is
-- strictly better. It is deliberately not created here because existing
-- production rows may already violate it, and this migration would then either
-- fail on deploy or require deleting live rows to proceed. Neither belongs in an
-- urgent change.
--
-- To adopt it later, first check:
--
--   SELECT user_id, bar_time, count(*)
--     FROM public.user_signals
--    GROUP BY 1, 2 HAVING count(*) > 1;
--
-- and if that returns nothing:
--
--   CREATE UNIQUE INDEX CONCURRENTLY user_signals_user_bar_unique
--     ON public.user_signals (user_id, bar_time);

COMMENT ON INDEX public.engine_config_execution_roster_idx IS
  'Serves /api/public/engine/users/active, the roster the multi-tenant executor '
  'polls. Partial on execution_mode <> OFF: most rows never need visiting.';
