
-- 1. open_positions table
CREATE TABLE IF NOT EXISTS public.open_positions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  trade_id text NOT NULL,
  side text,
  setup_name text,
  entry_t timestamptz,
  entry numeric,
  sl numeric,
  tp numeric,
  current_stop numeric,
  atr numeric,
  bars_held integer,
  prob numeric,
  threshold numeric,
  unrealized_pnl_rate numeric,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, trade_id)
);

GRANT SELECT ON public.open_positions TO authenticated;
GRANT ALL ON public.open_positions TO service_role;

ALTER TABLE public.open_positions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "open_positions_select_own" ON public.open_positions
  FOR SELECT TO authenticated USING (auth.uid() = user_id);

CREATE TRIGGER open_positions_touch
  BEFORE UPDATE ON public.open_positions
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

ALTER PUBLICATION supabase_realtime ADD TABLE public.open_positions;

-- 2. engine_config: capital_allocation_pct + relax leverage check
ALTER TABLE public.engine_config
  ADD COLUMN IF NOT EXISTS capital_allocation_pct numeric NOT NULL DEFAULT 100;

DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT conname FROM pg_constraint
    WHERE conrelid = 'public.engine_config'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) ILIKE '%leverage%'
  LOOP
    EXECUTE format('ALTER TABLE public.engine_config DROP CONSTRAINT %I', r.conname);
  END LOOP;
END$$;

ALTER TABLE public.engine_config
  ADD CONSTRAINT engine_config_leverage_check CHECK (leverage >= 1 AND leverage <= 70);

ALTER TABLE public.engine_config
  ADD CONSTRAINT engine_config_alloc_check CHECK (capital_allocation_pct >= 1 AND capital_allocation_pct <= 100);
