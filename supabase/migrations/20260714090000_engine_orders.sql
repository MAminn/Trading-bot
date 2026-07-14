-- =========================================================
-- ENGINE_ORDERS (executor writes order intents; user reads own)
-- =========================================================
CREATE TABLE public.engine_orders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  signal_bar_time timestamptz NOT NULL,
  symbol text NOT NULL DEFAULT 'ETHUSDT',
  side text NOT NULL CHECK (side IN ('LONG','SHORT')),
  intent text NOT NULL CHECK (intent IN ('OPEN','CLOSE')),
  qty numeric NOT NULL,
  ref_price numeric,
  notional_usd numeric,
  execution_mode text NOT NULL,
  status text NOT NULL DEFAULT 'INTENT_LOGGED' CHECK (status IN ('INTENT_LOGGED','DRYRUN','SENT','FILLED','FAILED','SKIPPED')),
  idempotency_key text NOT NULL UNIQUE,
  binance_order_id text,
  error text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX engine_orders_user_time_idx ON public.engine_orders(user_id, created_at DESC);
GRANT SELECT ON public.engine_orders TO authenticated;
GRANT ALL ON public.engine_orders TO service_role;
ALTER TABLE public.engine_orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own engine_orders select" ON public.engine_orders FOR SELECT TO authenticated USING (auth.uid() = user_id);
