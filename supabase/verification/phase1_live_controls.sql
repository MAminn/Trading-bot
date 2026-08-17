-- =========================================================
-- Phase 1 verification: engine_config live-control columns + privileges
-- =========================================================
-- Run against a STAGING database after applying:
--   20260817130000_engine_config_live_controls.sql
--   20260817131000_engine_config_column_privileges.sql
--
--   psql "$STAGING_DATABASE_URL" -v ON_ERROR_STOP=1 \
--        -f supabase/verification/phase1_live_controls.sql
--
-- The whole script runs inside one transaction and ends in ROLLBACK, so it
-- leaves no trace. It mutates an existing engine_config row rather than
-- inserting one, because user_id references auth.users.
--
-- Every check raises an exception on failure, so with ON_ERROR_STOP=1 the exit
-- code is the result: 0 = all invariants hold.

\set ON_ERROR_STOP on

BEGIN;

-- ---------------------------------------------------------
-- A. Schema shape
-- ---------------------------------------------------------
DO $$
DECLARE
  missing text;
BEGIN
  SELECT string_agg(c, ', ') INTO missing
  FROM unnest(ARRAY[
    'execution_mode','live_order_cap_usd','live_allow_full_capital','auto_execute_enabled'
  ]) AS c
  WHERE NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'engine_config' AND column_name = c
  );
  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'FAIL A1: missing columns: %', missing;
  END IF;
  RAISE NOTICE 'PASS A1: all four columns exist';
END $$;

DO $$
DECLARE
  nullable text;
BEGIN
  SELECT string_agg(column_name, ', ') INTO nullable
  FROM information_schema.columns
  WHERE table_schema = 'public' AND table_name = 'engine_config'
    AND column_name IN ('execution_mode','live_order_cap_usd','live_allow_full_capital')
    AND is_nullable = 'YES';
  IF nullable IS NOT NULL THEN
    -- A nullable control column would make its CHECK evaluate to NULL, which
    -- passes. That is an open gate, not a constraint.
    RAISE EXCEPTION 'FAIL A2: control columns must be NOT NULL: %', nullable;
  END IF;
  RAISE NOTICE 'PASS A2: control columns are NOT NULL';
END $$;

DO $$
DECLARE
  gen text;
BEGIN
  SELECT is_generated INTO gen FROM information_schema.columns
  WHERE table_schema = 'public' AND table_name = 'engine_config'
    AND column_name = 'auto_execute_enabled';
  IF gen IS DISTINCT FROM 'ALWAYS' THEN
    RAISE EXCEPTION 'FAIL A3: auto_execute_enabled must be GENERATED ALWAYS (got %)', gen;
  END IF;
  RAISE NOTICE 'PASS A3: auto_execute_enabled is generated, not independently writable';
END $$;

-- Defaults must be fail-closed for every pre-existing row.
DO $$
DECLARE
  bad bigint;
BEGIN
  SELECT count(*) INTO bad FROM public.engine_config
  WHERE execution_mode <> 'OFF'
     OR live_order_cap_usd <> 0
     OR live_allow_full_capital;
  IF bad > 0 THEN
    RAISE EXCEPTION 'FAIL A4: % existing row(s) did not land on fail-closed defaults', bad;
  END IF;
  RAISE NOTICE 'PASS A4: every existing row defaulted to OFF / cap 0 / no full-capital';
END $$;

-- ---------------------------------------------------------
-- B. Constraints reject illegal states
-- ---------------------------------------------------------
DO $$
DECLARE
  uid uuid;
BEGIN
  SELECT user_id INTO uid FROM public.engine_config ORDER BY created_at LIMIT 1;
  IF uid IS NULL THEN
    RAISE EXCEPTION 'SKIP: no engine_config row exists to test against';
  END IF;

  -- B1: unknown execution_mode
  BEGIN
    UPDATE public.engine_config SET execution_mode = 'TESTNET_TRADE' WHERE user_id = uid;
    RAISE EXCEPTION 'FAIL B1: an unknown execution_mode was accepted';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS B1: unknown execution_mode rejected';
  END;

  -- B2: cap above the executor's absolute ceiling
  BEGIN
    UPDATE public.engine_config SET live_order_cap_usd = 501 WHERE user_id = uid;
    RAISE EXCEPTION 'FAIL B2: a cap above 500 was accepted';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS B2: cap > 500 rejected';
  END;

  -- B3: negative cap
  BEGIN
    UPDATE public.engine_config SET live_order_cap_usd = -1 WHERE user_id = uid;
    RAISE EXCEPTION 'FAIL B3: a negative cap was accepted';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS B3: negative cap rejected';
  END;

  -- B4: LIVE_TRADE with a cap below the min-notional floor
  BEGIN
    UPDATE public.engine_config
       SET execution_mode = 'LIVE_TRADE', live_order_cap_usd = 10
     WHERE user_id = uid;
    RAISE EXCEPTION 'FAIL B4: LIVE_TRADE accepted with a cap below 25';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS B4: LIVE_TRADE with cap < 25 rejected';
  END;

  -- B5: LIVE_TRADE with a zero cap (the default) — the upgrade path must not
  -- be able to arm live trading by flipping one field.
  BEGIN
    UPDATE public.engine_config SET execution_mode = 'LIVE_TRADE' WHERE user_id = uid;
    RAISE EXCEPTION 'FAIL B5: LIVE_TRADE accepted with the default cap of 0';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS B5: LIVE_TRADE alone rejected; a cap must be set in the same statement';
  END;

  -- B6: full_capital on a live-trading config without the explicit flag
  BEGIN
    UPDATE public.engine_config
       SET execution_mode = 'LIVE_TRADE',
           live_order_cap_usd = 30,
           sizing_mode = 'full_capital',
           account_size_usd = 1000,
           live_allow_full_capital = false
     WHERE user_id = uid;
    RAISE EXCEPTION 'FAIL B6: live full_capital accepted without consent flag';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS B6: live full_capital rejected without live_allow_full_capital';
  END;

  -- B7: demo mode alongside live execution
  BEGIN
    UPDATE public.engine_config
       SET execution_mode = 'LIVE_TRADE', live_order_cap_usd = 30, demo_mode = true
     WHERE user_id = uid;
    RAISE EXCEPTION 'FAIL B7: demo_mode accepted alongside LIVE_TRADE';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS B7: demo_mode + LIVE_TRADE rejected';
  END;

  -- B8: demo mode alongside LIVE_READ (read-only is still live)
  BEGIN
    UPDATE public.engine_config
       SET execution_mode = 'LIVE_READ', demo_mode = true
     WHERE user_id = uid;
    RAISE EXCEPTION 'FAIL B8: demo_mode accepted alongside LIVE_READ';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS B8: demo_mode + LIVE_READ rejected';
  END;

  -- B9: the generated column may not be written directly
  BEGIN
    EXECUTE format(
      'UPDATE public.engine_config SET auto_execute_enabled = true WHERE user_id = %L', uid
    );
    RAISE EXCEPTION 'FAIL B9: auto_execute_enabled was directly writable';
  EXCEPTION WHEN generated_always THEN
    RAISE NOTICE 'PASS B9: auto_execute_enabled cannot be written directly';
  END;
END $$;

-- ---------------------------------------------------------
-- C. Legal states are accepted, and the generated column tracks mode
-- ---------------------------------------------------------
DO $$
DECLARE
  uid uuid;
  auto boolean;
BEGIN
  SELECT user_id INTO uid FROM public.engine_config ORDER BY created_at LIMIT 1;

  -- C1: the intended production state
  UPDATE public.engine_config
     SET execution_mode = 'LIVE_TRADE', live_order_cap_usd = 30, demo_mode = false
   WHERE user_id = uid;
  RAISE NOTICE 'PASS C1: LIVE_TRADE with a 30 USD cap accepted';

  -- C2: LIVE_READ needs no cap — it cannot place
  UPDATE public.engine_config
     SET execution_mode = 'LIVE_READ', live_order_cap_usd = 0
   WHERE user_id = uid;
  RAISE NOTICE 'PASS C2: LIVE_READ accepted with a zero cap';

  -- C3: live full_capital with consent
  UPDATE public.engine_config
     SET execution_mode = 'LIVE_TRADE',
         live_order_cap_usd = 30,
         sizing_mode = 'full_capital',
         account_size_usd = 1000,
         capital_usd = 1000,
         live_allow_full_capital = true
   WHERE user_id = uid;
  RAISE NOTICE 'PASS C3: live full_capital accepted with consent flag';

  -- C4: auto_execute_enabled follows mode, in both directions
  UPDATE public.engine_config SET mode = 'auto' WHERE user_id = uid;
  SELECT auto_execute_enabled INTO auto FROM public.engine_config WHERE user_id = uid;
  IF auto IS NOT TRUE THEN
    RAISE EXCEPTION 'FAIL C4a: mode=auto did not set auto_execute_enabled';
  END IF;
  UPDATE public.engine_config SET mode = 'signal_only' WHERE user_id = uid;
  SELECT auto_execute_enabled INTO auto FROM public.engine_config WHERE user_id = uid;
  IF auto IS NOT FALSE THEN
    RAISE EXCEPTION 'FAIL C4b: mode=signal_only did not clear auto_execute_enabled';
  END IF;
  RAISE NOTICE 'PASS C4: auto_execute_enabled tracks mode in both directions';
END $$;

-- ---------------------------------------------------------
-- D. Privileges: the four control columns are not user-writable
-- ---------------------------------------------------------
DO $$
DECLARE
  col text;
BEGIN
  -- The table-level UPDATE must be gone; otherwise every column stays writable
  -- and the column grants below are decorative.
  IF has_table_privilege('authenticated', 'public.engine_config', 'UPDATE') THEN
    RAISE EXCEPTION 'FAIL D1: authenticated still holds table-level UPDATE';
  END IF;
  RAISE NOTICE 'PASS D1: table-level UPDATE revoked from authenticated';

  FOREACH col IN ARRAY ARRAY['execution_mode','live_order_cap_usd','live_allow_full_capital','mode']
  LOOP
    IF has_column_privilege('authenticated', 'public.engine_config', col, 'UPDATE') THEN
      RAISE EXCEPTION 'FAIL D2: authenticated can update privileged column %', col;
    END IF;
  END LOOP;
  RAISE NOTICE 'PASS D2: all four privileged columns are not updatable by authenticated';

  -- The app's own write paths must still work.
  FOREACH col IN ARRAY ARRAY[
    'is_running','updated_at','capital_usd','account_size_usd','capital_allocation_pct',
    'leverage','sizing_mode','max_notional_usd','demo_mode'
  ]
  LOOP
    IF NOT has_column_privilege('authenticated', 'public.engine_config', col, 'UPDATE') THEN
      RAISE EXCEPTION 'FAIL D3: authenticated lost UPDATE on app-written column %', col;
    END IF;
  END LOOP;
  RAISE NOTICE 'PASS D3: every column the app writes is still updatable';

  -- The DELETE-then-INSERT bypass must be closed.
  IF has_table_privilege('authenticated', 'public.engine_config', 'DELETE') THEN
    RAISE EXCEPTION 'FAIL D4: authenticated can DELETE its row and re-INSERT arbitrary values';
  END IF;
  IF has_column_privilege('authenticated', 'public.engine_config', 'execution_mode', 'INSERT') THEN
    RAISE EXCEPTION 'FAIL D5: authenticated can INSERT an execution_mode';
  END IF;
  IF NOT has_column_privilege('authenticated', 'public.engine_config', 'user_id', 'INSERT') THEN
    RAISE EXCEPTION 'FAIL D6: authenticated cannot self-heal a missing config row';
  END IF;
  RAISE NOTICE 'PASS D4-D6: delete/insert bypass closed, self-heal preserved';

  -- The service role, which the server functions use, is unaffected.
  IF NOT has_column_privilege('service_role', 'public.engine_config', 'execution_mode', 'UPDATE') THEN
    RAISE EXCEPTION 'FAIL D7: service_role lost UPDATE on execution_mode';
  END IF;
  RAISE NOTICE 'PASS D7: service_role retains full write access';
END $$;

-- ---------------------------------------------------------
-- E. A real write attempt as `authenticated` is refused
-- ---------------------------------------------------------
-- Column privileges are checked before RLS, so this fails regardless of which
-- row would have matched.
SET LOCAL ROLE authenticated;

DO $$
BEGIN
  BEGIN
    UPDATE public.engine_config SET execution_mode = 'LIVE_TRADE';
    RAISE EXCEPTION 'FAIL E1: authenticated updated execution_mode directly';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'PASS E1: direct execution_mode write refused (insufficient_privilege)';
  END;

  BEGIN
    UPDATE public.engine_config SET live_order_cap_usd = 500;
    RAISE EXCEPTION 'FAIL E2: authenticated updated live_order_cap_usd directly';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'PASS E2: direct live_order_cap_usd write refused';
  END;

  BEGIN
    UPDATE public.engine_config SET live_allow_full_capital = true;
    RAISE EXCEPTION 'FAIL E3: authenticated updated live_allow_full_capital directly';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'PASS E3: direct live_allow_full_capital write refused';
  END;

  BEGIN
    UPDATE public.engine_config SET mode = 'auto';
    RAISE EXCEPTION 'FAIL E4: authenticated updated mode directly';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'PASS E4: direct mode write refused';
  END;
END $$;

RESET ROLE;

-- Nothing above is kept.
ROLLBACK;
