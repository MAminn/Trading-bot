-- Real Binance accounting is intentionally separate from strategy/user_trades.
-- Nothing in this table participates in signal generation, sizing, risk gates,
-- order placement, reconciliation, or any other trading decision.

CREATE TABLE public.executed_trades (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  symbol text NOT NULL DEFAULT 'ETHUSDT',
  side text NOT NULL CHECK (side IN ('LONG', 'SHORT')),

  open_binance_order_id text NOT NULL,
  close_binance_order_id text NOT NULL,
  entry_time timestamptz NOT NULL,
  exit_time timestamptz NOT NULL,
  qty numeric NOT NULL CHECK (qty > 0),
  entry_avg_price numeric NOT NULL CHECK (entry_avg_price > 0),
  exit_avg_price numeric NOT NULL CHECK (exit_avg_price > 0),

  entry_fill_count integer NOT NULL DEFAULT 0 CHECK (entry_fill_count >= 0),
  exit_fill_count integer NOT NULL DEFAULT 0 CHECK (exit_fill_count >= 0),

  -- Binance-reported values only. Do not derive these from strategy returns.
  gross_pnl_usd numeric NOT NULL,
  entry_commission_usd numeric NOT NULL DEFAULT 0 CHECK (entry_commission_usd >= 0),
  exit_commission_usd numeric NOT NULL DEFAULT 0 CHECK (exit_commission_usd >= 0),
  commission_usd numeric GENERATED ALWAYS AS
    (entry_commission_usd + exit_commission_usd) STORED,
  -- Binance income history uses positive for funding received and negative for
  -- funding paid. Keep the sign exactly as returned by Binance.
  funding_usd numeric NOT NULL DEFAULT 0,
  net_pnl_usd numeric GENERATED ALWAYS AS
    (gross_pnl_usd - entry_commission_usd - exit_commission_usd + funding_usd) STORED,

  source text NOT NULL DEFAULT 'BINANCE' CHECK (source = 'BINANCE'),
  synced_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE (user_id, close_binance_order_id)
);

CREATE INDEX executed_trades_user_exit_idx
  ON public.executed_trades(user_id, exit_time DESC);

GRANT SELECT ON public.executed_trades TO authenticated;
GRANT ALL ON public.executed_trades TO service_role;

ALTER TABLE public.executed_trades ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own executed_trades select"
  ON public.executed_trades
  FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

COMMENT ON TABLE public.executed_trades IS
  'Read-only customer reporting of actual Binance fills, commission, funding and realized net P&L. Never used by trading logic.';
