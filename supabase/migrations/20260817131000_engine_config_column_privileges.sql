-- =========================================================
-- Column-level write control on engine_config
-- =========================================================
-- engine_config is directly writable by `authenticated` over PostgREST: RLS
-- restricts which ROW you may touch, never which COLUMN. Every server-side
-- validator in the app is therefore optional from the database's point of
-- view — a user can PATCH /rest/v1/engine_config and bypass it entirely.
--
-- That is survivable for sizing fields, which CHECK constraints already bound.
-- It is not survivable for the live-execution controls: execution_mode,
-- live_order_cap_usd, live_allow_full_capital and mode decide whether real
-- orders may be placed and how large they may be. Those four become
-- server-function-only, written with the service role after validation.
--
-- IMPORTANT — why the table-level grant is dropped rather than narrowed:
-- `REVOKE UPDATE (col) ON t FROM role` does NOT override a table-level
-- `GRANT UPDATE ON t`. PostgreSQL emits a warning and leaves the table-level
-- privilege in place, so the protection would appear to be applied while
-- silently not existing. The only correct form is to drop the table-level
-- privilege and re-grant an explicit column list.
REVOKE UPDATE ON public.engine_config FROM authenticated;

-- Exactly the columns the app's two user-JWT write paths need:
--   setEngineRunning   -> is_running, updated_at
--   updateEngineConfig -> the sizing fields, demo_mode, updated_at
-- Everything omitted is either privileged (the four above), structural
-- (id, user_id, created_at) or generated (auto_execute_enabled).
GRANT UPDATE (
  capital_usd,
  account_size_usd,
  capital_allocation_pct,
  leverage,
  sizing_mode,
  max_notional_usd,
  max_daily_loss_usd,
  max_position_size_usd,
  is_running,
  demo_mode,
  updated_at
) ON public.engine_config TO authenticated;

-- Closing the way around the above: `authenticated` held table-level INSERT
-- and DELETE. DELETE-then-INSERT would let a user replace their own row with
-- one carrying any execution_mode and any cap, which would defeat the column
-- restriction completely. Nothing in the app inserts or deletes this row —
-- it is created by the on-signup trigger (SECURITY DEFINER, unaffected here)
-- and removed by the ON DELETE CASCADE from auth.users.
REVOKE INSERT, DELETE ON public.engine_config FROM authenticated;

-- A user may still self-heal a missing row; every other column then takes its
-- fail-closed default (execution_mode 'OFF', cap 0, no full-capital consent).
GRANT INSERT (user_id) ON public.engine_config TO authenticated;

-- service_role keeps GRANT ALL from the original migration: the server
-- functions that will write the privileged columns in a later phase go
-- through it, after validation.
