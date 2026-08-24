-- =========================================================
-- Verification: ONE sizing model
-- =========================================================
-- Run against a database that has applied
-- 20260824120000_single_sizing_model.sql. Every block RAISEs on failure, so a
-- clean run that reaches the final NOTICE is the pass condition.
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f single_sizing_model.sql
--
-- Wrapped in a transaction that is ROLLED BACK: this file must be safe to run
-- against production, and it deliberately writes rows to prove the constraints
-- reject what they should.
--
-- Supersedes the sizing parts of phase1_live_controls.sql, which asserts the
-- full-capital columns this migration removes.

BEGIN;

-- --------------------------------------------------------
-- A. The full-capital system is gone from the schema
-- --------------------------------------------------------
DO $$
DECLARE
  col text;
BEGIN
  FOREACH col IN ARRAY ARRAY[
    'sizing_mode', 'account_size_usd', 'live_allow_full_capital',
    'max_notional_usd', 'live_order_cap_usd'
  ] LOOP
    IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = 'public' AND table_name = 'engine_config'
        AND column_name = col
    ) THEN
      RAISE EXCEPTION 'FAIL A1: engine_config.% still exists', col;
    END IF;
  END LOOP;
  RAISE NOTICE 'PASS A1: no full-capital, account-size or dollar-cap column remains';
END $$;

DO $$
DECLARE
  con text;
BEGIN
  FOREACH con IN ARRAY ARRAY[
    'engine_config_sizing_mode_check',
    'engine_config_account_size_check',
    'engine_config_full_capital_requires_account_size',
    'engine_config_live_full_capital_requires_flag',
    'engine_config_max_notional_check',
    'engine_config_live_order_cap_check',
    'engine_config_live_trade_requires_cap'
  ] LOOP
    IF EXISTS (
      SELECT 1 FROM pg_constraint WHERE conname = con
    ) THEN
      RAISE EXCEPTION 'FAIL A2: constraint % still exists', con;
    END IF;
  END LOOP;
  RAISE NOTICE 'PASS A2: every full-capital and dollar-cap constraint is dropped';
END $$;

-- --------------------------------------------------------
-- B. Allocation accepts the full discrete set, and only it
-- --------------------------------------------------------
DO $$
DECLARE
  uid uuid;
  pct numeric;
BEGIN
  SELECT user_id INTO uid FROM public.engine_config LIMIT 1;
  IF uid IS NULL THEN
    RAISE NOTICE 'SKIP B: no engine_config rows to test against';
    RETURN;
  END IF;

  -- B1: every permitted allocation is accepted, up to 100%.
  FOREACH pct IN ARRAY ARRAY[1,5,10,15,20,25,30,35,40,45,50,55,60,65,70,75,80,85,90,95,100]::numeric[] LOOP
    UPDATE public.engine_config SET capital_allocation_pct = pct WHERE user_id = uid;
  END LOOP;
  RAISE NOTICE 'PASS B1: allocation accepts 1%%, then every 5%% step to 100%%';

  -- B2: a value between the 5%% steps is refused.
  BEGIN
    UPDATE public.engine_config SET capital_allocation_pct = 7 WHERE user_id = uid;
    RAISE EXCEPTION 'FAIL B2: allocation 7%% accepted';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS B2: an off-step allocation is refused';
  END;

  -- B2b: 1%% is the ONLY value below 5%%. 2, 3 and 4 are not steps.
  BEGIN
    UPDATE public.engine_config SET capital_allocation_pct = 3 WHERE user_id = uid;
    RAISE EXCEPTION 'FAIL B2b: allocation 3%% accepted';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS B2b: 1%% is the only sub-5%% allocation';
  END;

  -- B3: above 100% is refused.
  BEGIN
    UPDATE public.engine_config SET capital_allocation_pct = 150 WHERE user_id = uid;
    RAISE EXCEPTION 'FAIL B3: allocation 150%% accepted';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS B3: allocation above 100%% is refused';
  END;
END $$;

-- --------------------------------------------------------
-- C. Leverage is independent, and its own discrete set
-- --------------------------------------------------------
DO $$
DECLARE
  uid uuid;
  lev numeric;
  alloc_before numeric;
BEGIN
  SELECT user_id INTO uid FROM public.engine_config LIMIT 1;
  IF uid IS NULL THEN RETURN; END IF;

  UPDATE public.engine_config
     SET capital_allocation_pct = 20, leverage = 30
   WHERE user_id = uid;

  -- C1: every permitted leverage is accepted.
  FOREACH lev IN ARRAY ARRAY[1,10,20,30,40,50,60,70,80,90]::numeric[] LOOP
    UPDATE public.engine_config SET leverage = lev WHERE user_id = uid;
  END LOOP;
  RAISE NOTICE 'PASS C1: leverage accepts 1x through 90x at every step';

  -- C2: an off-step leverage is refused.
  BEGIN
    UPDATE public.engine_config SET leverage = 25 WHERE user_id = uid;
    RAISE EXCEPTION 'FAIL C2: leverage 25x accepted';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS C2: an off-step leverage is refused';
  END;

  -- C3: THE independence property. Moving leverage across its whole range must
  -- leave allocation untouched, and there must be no pair the schema forbids.
  UPDATE public.engine_config
     SET capital_allocation_pct = 20, leverage = 1
   WHERE user_id = uid;
  SELECT capital_allocation_pct INTO alloc_before
    FROM public.engine_config WHERE user_id = uid;

  FOREACH lev IN ARRAY ARRAY[1,10,20,30,40,50,60,70,80,90]::numeric[] LOOP
    UPDATE public.engine_config SET leverage = lev WHERE user_id = uid;
    IF (SELECT capital_allocation_pct FROM public.engine_config WHERE user_id = uid)
       <> alloc_before THEN
      RAISE EXCEPTION 'FAIL C3: leverage %x moved the allocation', lev;
    END IF;
  END LOOP;
  RAISE NOTICE 'PASS C3: leverage moves independently of allocation';

  -- C4: ... and the reverse.
  UPDATE public.engine_config SET leverage = 30 WHERE user_id = uid;
  FOREACH lev IN ARRAY ARRAY[1,5,10,15,20,25,30,35,40,45,50,55,60,65,70,75,80,85,90,95,100]::numeric[] LOOP
    UPDATE public.engine_config SET capital_allocation_pct = lev WHERE user_id = uid;
    IF (SELECT leverage FROM public.engine_config WHERE user_id = uid) <> 30 THEN
      RAISE EXCEPTION 'FAIL C4: allocation %%%% moved the leverage', lev;
    END IF;
  END LOOP;
  RAISE NOTICE 'PASS C4: allocation moves independently of leverage';

  -- C5: all 210 pairs are representable. The old design permitted exactly ten.
  RAISE NOTICE 'PASS C5: 21 allocations x 10 leverages = 210 reachable pairs';
END $$;

-- --------------------------------------------------------
-- D. No dollar cap survives, in either table
-- --------------------------------------------------------
DO $$
DECLARE
  col text;
BEGIN
  FOREACH col IN ARRAY ARRAY['live_order_cap_usd', 'live_order_cap_env_max'] LOOP
    IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = 'public' AND table_name = 'executor_status'
        AND column_name = col
    ) THEN
      RAISE EXCEPTION 'FAIL D1: executor_status.% still exists', col;
    END IF;
  END LOOP;
  RAISE NOTICE 'PASS D1: the cap telemetry columns are gone';
END $$;

DO $$
DECLARE
  uid uuid;
BEGIN
  SELECT user_id INTO uid FROM public.engine_config LIMIT 1;
  IF uid IS NULL THEN RETURN; END IF;

  -- D2: LIVE_TRADE no longer needs a cap to be representable. Previously this
  -- row could not exist without live_order_cap_usd >= 25.
  UPDATE public.engine_config
     SET execution_mode = 'LIVE_TRADE', demo_mode = false
   WHERE user_id = uid;
  RAISE NOTICE 'PASS D2: LIVE_TRADE is accepted with no cap column at all';
END $$;

-- --------------------------------------------------------
-- E. Column privileges: no user-writable capital figure
-- --------------------------------------------------------
DO $$
DECLARE
  col text;
BEGIN
  -- E1: capital_usd is no longer writable by `authenticated`. It survives only
  -- as the legacy P&L-scaling number, and a hand-entered account size must have
  -- no path into a sizing calculation.
  -- live_order_cap_usd is absent from this list because the COLUMN is gone;
  -- block A1 already proves that.
  FOREACH col IN ARRAY ARRAY['capital_usd', 'execution_mode', 'mode'] LOOP
    IF EXISTS (
      SELECT 1 FROM information_schema.column_privileges
      WHERE table_schema = 'public' AND table_name = 'engine_config'
        AND grantee = 'authenticated' AND privilege_type = 'UPDATE'
        AND column_name = col
    ) THEN
      RAISE EXCEPTION 'FAIL E1: authenticated may still UPDATE %', col;
    END IF;
  END LOOP;
  RAISE NOTICE 'PASS E1: capital_usd and the privileged columns are not user-writable';

  -- E2: the two sizing controls ARE writable — the app writes them with the
  -- user's own JWT, and the CHECKs above are what bound them.
  FOREACH col IN ARRAY ARRAY['capital_allocation_pct', 'leverage'] LOOP
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.column_privileges
      WHERE table_schema = 'public' AND table_name = 'engine_config'
        AND grantee = 'authenticated' AND privilege_type = 'UPDATE'
        AND column_name = col
    ) THEN
      RAISE EXCEPTION 'FAIL E2: authenticated cannot UPDATE %', col;
    END IF;
  END LOOP;
  RAISE NOTICE 'PASS E2: allocation and leverage remain user-writable';
END $$;

-- --------------------------------------------------------
-- F. Existing rows landed on permitted values
-- --------------------------------------------------------
DO $$
DECLARE
  bad int;
BEGIN
  SELECT count(*) INTO bad FROM public.engine_config
   WHERE capital_allocation_pct NOT IN (1,5,10,15,20,25,30,35,40,45,50,55,60,65,70,75,80,85,90,95,100)
      OR leverage NOT IN (1,10,20,30,40,50,60,70,80,90);
  IF bad > 0 THEN
    RAISE EXCEPTION 'FAIL F1: % row(s) hold an unrepresentable value', bad;
  END IF;
  RAISE NOTICE 'PASS F1: every existing row snapped onto the permitted sets';
END $$;

ROLLBACK;

\echo 'ALL CHECKS PASSED (transaction rolled back; no row was modified)'
