-- =========================================================
-- executor_status: report the live-control decision, not just its outcome
-- =========================================================
-- Phase 3 lets the executor combine the database request with its own
-- environment ceiling. Three separate facts result, and collapsing them into
-- one column is how a silently degraded request goes unnoticed:
--
--   db_execution_mode  what the database asked for
--   env_mode_ceiling   what this host permits at most   (already present)
--   effective_mode     what is actually running          (already present)
--
-- Telemetry only, exactly as in the original table: nothing here is read by
-- the executor, and no column added below controls anything.
ALTER TABLE public.executor_status
  ADD COLUMN IF NOT EXISTS db_execution_mode text
    CHECK (db_execution_mode IS NULL
           OR db_execution_mode IN ('OFF','LIVE_READ','LIVE_TRADE'));

ALTER TABLE public.executor_status
  ADD COLUMN IF NOT EXISTS auto_execute_enabled boolean;

-- The cap actually in force, and the host ceiling it was clamped against.
-- Reporting both makes "the database asked for more than this host allows"
-- visible instead of inferable.
ALTER TABLE public.executor_status
  ADD COLUMN IF NOT EXISTS live_order_cap_usd numeric;
ALTER TABLE public.executor_status
  ADD COLUMN IF NOT EXISTS live_order_cap_env_max numeric;

-- Whether an OPEN could be placed at the moment of this heartbeat, and if not,
-- which gate refused. Nullable: an executor that has not decided yet reports
-- null rather than a fabricated false.
ALTER TABLE public.executor_status
  ADD COLUMN IF NOT EXISTS orders_enabled boolean;
ALTER TABLE public.executor_status
  ADD COLUMN IF NOT EXISTS blocked_reason text;
