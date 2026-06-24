
-- =========================================================
-- ENGINE_CONFIG
-- =========================================================
CREATE TABLE public.engine_config (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
  mode text NOT NULL DEFAULT 'signal_only' CHECK (mode IN ('signal_only','auto')),
  capital_usd numeric NOT NULL DEFAULT 10000,
  leverage numeric NOT NULL DEFAULT 1,
  max_daily_loss_usd numeric NOT NULL DEFAULT 100,
  max_position_size_usd numeric NOT NULL DEFAULT 500,
  is_running boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE, DELETE ON public.engine_config TO authenticated;
GRANT ALL ON public.engine_config TO service_role;
ALTER TABLE public.engine_config ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own engine_config select" ON public.engine_config FOR SELECT TO authenticated USING (auth.uid() = user_id);
CREATE POLICY "own engine_config insert" ON public.engine_config FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own engine_config update" ON public.engine_config FOR UPDATE TO authenticated USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- =========================================================
-- ENGINE_STATUS (per user, written by engine)
-- =========================================================
CREATE TABLE public.engine_status (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
  status text NOT NULL DEFAULT 'stopped' CHECK (status IN ('running','stopped','error')),
  last_heartbeat timestamptz,
  current_position text NOT NULL DEFAULT 'FLAT' CHECK (current_position IN ('FLAT','LONG','SHORT')),
  message text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT ON public.engine_status TO authenticated;
GRANT ALL ON public.engine_status TO service_role;
ALTER TABLE public.engine_status ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own engine_status select" ON public.engine_status FOR SELECT TO authenticated USING (auth.uid() = user_id);

-- =========================================================
-- USER_SIGNALS (engine writes; user reads own)
-- =========================================================
CREATE TABLE public.user_signals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  bar_time timestamptz NOT NULL,
  bar_closed_now boolean,
  valid_next_entry boolean,
  rule_side integer,
  rule_reason text,
  ml_prob numeric,
  ml_threshold numeric,
  ml_accept boolean,
  opened text,
  closed_reason text,
  position_before text,
  position_after text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX user_signals_user_time_idx ON public.user_signals(user_id, created_at DESC);
GRANT SELECT ON public.user_signals TO authenticated;
GRANT ALL ON public.user_signals TO service_role;
ALTER TABLE public.user_signals ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own user_signals select" ON public.user_signals FOR SELECT TO authenticated USING (auth.uid() = user_id);

-- =========================================================
-- USER_TRADES
-- =========================================================
CREATE TABLE public.user_trades (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  trade_id text,
  side text,
  setup_name text,
  signal_t timestamptz,
  entry_t timestamptz,
  exit_t timestamptz,
  entry numeric,
  exit numeric,
  tp numeric,
  sl numeric,
  final_stop numeric,
  atr numeric,
  bars_held integer,
  prob numeric,
  threshold numeric,
  exit_reason text,
  net_pnl_rate numeric,
  round_trip_cost numeric,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX user_trades_user_exit_idx ON public.user_trades(user_id, exit_t DESC NULLS LAST);
GRANT SELECT ON public.user_trades TO authenticated;
GRANT ALL ON public.user_trades TO service_role;
ALTER TABLE public.user_trades ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own user_trades select" ON public.user_trades FOR SELECT TO authenticated USING (auth.uid() = user_id);

-- =========================================================
-- BINANCE_KEYS (encrypted; secret never selectable from client)
-- =========================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE public.binance_keys (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
  api_key_encrypted bytea NOT NULL,
  api_secret_encrypted bytea NOT NULL,
  api_key_last4 text NOT NULL,
  permissions_note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
-- No grants to authenticated on the raw table — encrypted blobs stay server-only.
GRANT ALL ON public.binance_keys TO service_role;
ALTER TABLE public.binance_keys ENABLE ROW LEVEL SECURITY;
-- (no policies — authenticated cannot select directly)

-- Safe view for the browser: only last4 + metadata
CREATE VIEW public.binance_keys_safe
WITH (security_invoker = true)
AS SELECT user_id, api_key_last4, permissions_note, created_at, updated_at
   FROM public.binance_keys
   WHERE user_id = auth.uid();
GRANT SELECT ON public.binance_keys_safe TO authenticated;

-- Encrypt helper (SECURITY DEFINER so the secret never leaves the server).
-- The encryption passphrase comes from a Postgres GUC set on the role:
--   ALTER ROLE postgres SET app.binance_secret = '...';
-- We read it via current_setting('app.binance_secret', true).
CREATE OR REPLACE FUNCTION public.save_binance_keys(_api_key text, _api_secret text, _note text DEFAULT NULL)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  uid uuid := auth.uid();
  pass text := current_setting('app.binance_secret', true);
BEGIN
  IF uid IS NULL THEN RAISE EXCEPTION 'not authenticated'; END IF;
  IF pass IS NULL OR pass = '' THEN RAISE EXCEPTION 'encryption secret not configured'; END IF;
  IF length(_api_key) < 8 OR length(_api_secret) < 8 THEN RAISE EXCEPTION 'invalid keys'; END IF;

  INSERT INTO public.binance_keys(user_id, api_key_encrypted, api_secret_encrypted, api_key_last4, permissions_note)
  VALUES (uid,
          pgp_sym_encrypt(_api_key, pass),
          pgp_sym_encrypt(_api_secret, pass),
          right(_api_key, 4),
          _note)
  ON CONFLICT (user_id) DO UPDATE
    SET api_key_encrypted = EXCLUDED.api_key_encrypted,
        api_secret_encrypted = EXCLUDED.api_secret_encrypted,
        api_key_last4 = EXCLUDED.api_key_last4,
        permissions_note = EXCLUDED.permissions_note,
        updated_at = now();
END;
$$;
REVOKE ALL ON FUNCTION public.save_binance_keys(text, text, text) FROM public;
GRANT EXECUTE ON FUNCTION public.save_binance_keys(text, text, text) TO authenticated;

CREATE OR REPLACE FUNCTION public.delete_binance_keys()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  IF auth.uid() IS NULL THEN RAISE EXCEPTION 'not authenticated'; END IF;
  DELETE FROM public.binance_keys WHERE user_id = auth.uid();
END;
$$;
REVOKE ALL ON FUNCTION public.delete_binance_keys() FROM public;
GRANT EXECUTE ON FUNCTION public.delete_binance_keys() TO authenticated;

-- Service-role-only decrypt (called from the engine via service-role client)
CREATE OR REPLACE FUNCTION public.decrypt_binance_keys_for(_user_id uuid)
RETURNS TABLE(api_key text, api_secret text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  pass text := current_setting('app.binance_secret', true);
BEGIN
  IF pass IS NULL OR pass = '' THEN RAISE EXCEPTION 'encryption secret not configured'; END IF;
  RETURN QUERY
  SELECT pgp_sym_decrypt(api_key_encrypted, pass),
         pgp_sym_decrypt(api_secret_encrypted, pass)
  FROM public.binance_keys
  WHERE user_id = _user_id;
END;
$$;
REVOKE ALL ON FUNCTION public.decrypt_binance_keys_for(uuid) FROM public;
GRANT EXECUTE ON FUNCTION public.decrypt_binance_keys_for(uuid) TO service_role;

-- =========================================================
-- New-user defaults: engine_config + engine_status
-- =========================================================
CREATE OR REPLACE FUNCTION public.handle_new_user_engine()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  INSERT INTO public.engine_config(user_id) VALUES (NEW.id) ON CONFLICT DO NOTHING;
  INSERT INTO public.engine_status(user_id) VALUES (NEW.id) ON CONFLICT DO NOTHING;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created_engine ON auth.users;
CREATE TRIGGER on_auth_user_created_engine
AFTER INSERT ON auth.users
FOR EACH ROW EXECUTE FUNCTION public.handle_new_user_engine();

-- Backfill defaults for existing users
INSERT INTO public.engine_config(user_id)
SELECT id FROM auth.users
ON CONFLICT DO NOTHING;
INSERT INTO public.engine_status(user_id)
SELECT id FROM auth.users
ON CONFLICT DO NOTHING;

-- updated_at trigger for engine_config
CREATE OR REPLACE FUNCTION public.touch_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END; $$;
CREATE TRIGGER engine_config_touch BEFORE UPDATE ON public.engine_config
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();
CREATE TRIGGER engine_status_touch BEFORE UPDATE ON public.engine_status
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();
CREATE TRIGGER binance_keys_touch BEFORE UPDATE ON public.binance_keys
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

-- Realtime
ALTER PUBLICATION supabase_realtime ADD TABLE public.engine_status;
ALTER PUBLICATION supabase_realtime ADD TABLE public.user_signals;
ALTER PUBLICATION supabase_realtime ADD TABLE public.user_trades;
ALTER PUBLICATION supabase_realtime ADD TABLE public.engine_config;
