-- =========================================================
-- EXECUTOR_STATUS (Binance executor telemetry; executor writes, user reads own)
-- =========================================================
-- Deliberately NOT engine_status: that row is unique per user and is owned by
-- the ML signal worker's heartbeat thread. A second writer would clobber it,
-- and the two services report genuinely different things — engine_status is the
-- strategy's paper position, this is the exchange's real one.
--
-- Telemetry only. No column here controls execution; the executor's live
-- capability still comes exclusively from its own environment.
CREATE TABLE public.executor_status (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,

  -- What the executor is actually running, and the ceiling its env permits.
  -- Equal today; they diverge once the DB may narrow the mode.
  effective_mode text NOT NULL
    CHECK (effective_mode IN ('OFF','TESTNET_READ','TESTNET_TRADE','LIVE_READ','LIVE_TRADE')),
  env_mode_ceiling text
    CHECK (env_mode_ceiling IS NULL OR env_mode_ceiling IN ('OFF','TESTNET_READ','TESTNET_TRADE','LIVE_READ','LIVE_TRADE')),

  -- Account snapshot, straight from the exchange. Nullable throughout: a
  -- pre-first-fetch or read-failed cycle reports null, never a fabricated 0.
  wallet_balance_usd numeric,
  available_balance_usd numeric,

  -- Position snapshot for the traded symbol.
  position_amt numeric,
  position_side text CHECK (position_side IS NULL OR position_side IN ('FLAT','LONG','SHORT')),
  entry_price numeric,
  position_leverage numeric,
  margin_type text,

  -- Last reconcile outcome (app's expected position vs. the exchange's actual).
  reconcile_match boolean,
  reconcile_expected numeric,
  reconcile_actual numeric,
  last_reconcile_at timestamptz,

  -- Credential state. Presence only — no key material is ever stored here.
  keys_present boolean,
  permission_status text
    CHECK (permission_status IS NULL OR permission_status IN ('verified_futures','unknown','failed')),

  message text,
  last_heartbeat timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT ON public.executor_status TO authenticated;
GRANT ALL    ON public.executor_status TO service_role;

ALTER TABLE public.executor_status ENABLE ROW LEVEL SECURITY;
-- Read-own only. Writes arrive exclusively through the service-role ingest
-- route, so there is no INSERT/UPDATE policy for authenticated by design.
CREATE POLICY "own executor_status select" ON public.executor_status
  FOR SELECT TO authenticated USING (auth.uid() = user_id);

CREATE TRIGGER executor_status_touch BEFORE UPDATE ON public.executor_status
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

ALTER PUBLICATION supabase_realtime ADD TABLE public.executor_status;
