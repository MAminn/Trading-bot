-- =========================================================
-- STEP 2 of the rollout: pre-deploy normalization
-- =========================================================
-- Run this against production BEFORE deploying the new app/executor build and
-- BEFORE applying 20260824120000_single_sizing_model.sql.
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f pre_deploy_normalize.sql
--
-- Why it exists
-- -------------
-- Between the app deploy and the migration there is a window in which the NEW
-- app talks to the OLD schema. The new app no longer writes sizing_mode or
-- live_order_cap_usd, but the old schema still carries two CHECK constraints
-- that consult them:
--
--   engine_config_live_trade_requires_cap
--     CHECK (execution_mode <> 'LIVE_TRADE' OR live_order_cap_usd >= 25)
--   engine_config_live_full_capital_requires_flag
--     CHECK (execution_mode <> 'LIVE_TRADE' OR sizing_mode <> 'full_capital'
--            OR live_allow_full_capital)
--
-- A user who switches to LIVE_TRADE during that window, on a row with a cap
-- below 25 or an unconsented full_capital, would get a raw constraint violation
-- instead of a saved setting. This script moves every row to a state both
-- constraints already accept, so the window has no failure mode at all.
--
-- It is deliberately conservative: every write here makes the OLD executor size
-- SMALLER or leaves it unchanged. Nothing widens exposure, and nothing touches
-- an open position.

BEGIN;

-- 1. Retire full-capital sizing ahead of the schema change.
--    The old executor then uses the capped allocation path — the narrower of
--    the two — for the few minutes before the new build takes over.
UPDATE public.engine_config
   SET sizing_mode = 'allocation'
 WHERE sizing_mode <> 'allocation';

-- 2. Satisfy the old minimum-cap constraint unconditionally. 500 is the old
--    schema's own maximum, so this is the largest value it can hold; the old
--    executor was already bounded by HARD_CAP_USD = 500 regardless, and the new
--    executor ignores this column entirely before the migration drops it.
UPDATE public.engine_config
   SET live_order_cap_usd = 500
 WHERE live_order_cap_usd IS DISTINCT FROM 500;

-- 3. Report what the migration's snap-down will do, BEFORE it happens, so the
--    operator sees any user whose configured size is about to change.
DO $$
DECLARE
  r record;
  n int := 0;
BEGIN
  FOR r IN
    SELECT user_id, capital_allocation_pct AS pct, leverage AS lev
      FROM public.engine_config
     WHERE capital_allocation_pct NOT IN (1,5,10,15,20,25,30,35,40,45,50,55,60,65,70,75,80,85,90,95,100)
        OR leverage NOT IN (1,10,20,30,40,50,60,70,80,90)
  LOOP
    n := n + 1;
    RAISE NOTICE
      'WILL SNAP DOWN | user=% | allocation %%%% -> nearest lower step | leverage %x -> nearest lower step',
      r.user_id, r.pct, r.lev;
  END LOOP;
  IF n = 0 THEN
    RAISE NOTICE 'PASS: every row already sits on a permitted allocation and leverage';
  ELSE
    RAISE NOTICE '% row(s) will be snapped DOWN by the migration (never up)', n;
  END IF;
END $$;

-- 4. Show the live state the maintenance window is starting from.
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT user_id, execution_mode, mode, is_running,
           capital_allocation_pct, leverage
      FROM public.engine_config
     WHERE execution_mode <> 'OFF'
  LOOP
    RAISE NOTICE
      'LIVE ROW | user=% | execution_mode=% | mode=% | is_running=% | %%%% alloc | %x',
      r.user_id, r.execution_mode, r.mode, r.is_running,
      r.capital_allocation_pct, r.leverage;
  END LOOP;
END $$;

COMMIT;

\echo 'Pre-deploy normalization complete. Deploy the app + executor, then apply the migration.'
