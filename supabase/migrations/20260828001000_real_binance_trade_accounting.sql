-- =========================================================
-- REAL BINANCE COMPLETED-TRADE ACCOUNTING
-- =========================================================
--
-- This is the THIRD and last layer of the trade record, and the only one that
-- describes the client's actual money:
--
--   1. user_trades     what the STRATEGY did. net_pnl_rate is a fractional
--                      return; every USD figure derived from it is
--                      capital_usd x rate, i.e. MODELLED, not realised.
--   2. engine_orders   what the EXECUTOR sent to Binance, and whether it filled.
--   3. executed_trades (this table) what BINANCE actually reported: realised
--                      P&L, the exact commission charged on every fill, and the
--                      funding paid or received while the position was open.
--
-- Nothing in this table participates in signal generation, sizing, leverage,
-- risk gates, order placement or reconciliation. It is written only by the
-- read-only accounting synchroniser (executor/accounting_sync.py) through the
-- service-role endpoint, and read only by the owning customer.
--
-- Money columns are NUMERIC. None of them is ever computed from a fee
-- percentage, an assumed rate, or a strategy return.

CREATE TABLE public.executed_trades (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  symbol text NOT NULL DEFAULT 'ETHUSDT',
  side text NOT NULL CHECK (side IN ('LONG', 'SHORT')),

  -- The Binance order that opened the position, and the one that finally
  -- flattened it. A Binance MARKET order can produce many fills; these identify
  -- the ORDERS, and the fill counts below record how many fills were aggregated
  -- into each average.
  open_binance_order_id text NOT NULL,
  close_binance_order_id text NOT NULL,

  entry_time timestamptz NOT NULL,
  exit_time timestamptz NOT NULL,

  qty numeric CHECK (qty IS NULL OR qty > 0),
  entry_avg_price numeric CHECK (entry_avg_price IS NULL OR entry_avg_price > 0),
  exit_avg_price numeric CHECK (exit_avg_price IS NULL OR exit_avg_price > 0),
  entry_fill_count integer NOT NULL DEFAULT 0 CHECK (entry_fill_count >= 0),
  exit_fill_count integer NOT NULL DEFAULT 0 CHECK (exit_fill_count >= 0),
  -- More than one when the position was closed in stages — a partial scale-out,
  -- or the client closing part of it by hand and Helix closing the remainder.
  exit_order_count integer NOT NULL DEFAULT 0 CHECK (exit_order_count >= 0),

  -- How the position actually left the account. Helix opened it in every case
  -- (positions opened by the client are never recorded here), but it can be
  -- closed by the bot, by the client in the Binance app, by an exchange-side
  -- stop, or by some combination. All three are fully accounted — the fills are
  -- the monetary truth either way — and the customer is told which happened.
  close_source text NOT NULL DEFAULT 'HELIX'
    CHECK (close_source IN ('HELIX', 'EXTERNAL', 'MIXED')),

  -- Binance-reported values only.
  --
  -- gross_pnl_usd is the sum of realizedPnl across the closing order's fills.
  -- Binance reports realizedPnl BEFORE commission and BEFORE funding, which is
  -- exactly the "gross" the customer is shown.
  gross_pnl_usd numeric,
  -- The sum of commission across each side's fills, and only when every one of
  -- those fills was charged in USDT. A commission charged in another asset is
  -- never converted at a guessed rate: the row is marked INCOMPLETE instead.
  entry_commission_usd numeric CHECK (entry_commission_usd IS NULL OR entry_commission_usd >= 0),
  exit_commission_usd numeric CHECK (exit_commission_usd IS NULL OR exit_commission_usd >= 0),
  -- Generated, so the total a customer sees can never disagree with the two
  -- halves it is made of. There is one authoritative calculation and it lives
  -- here, in the database, not in the synchroniser and not in the UI.
  commission_usd numeric GENERATED ALWAYS AS
    (entry_commission_usd + exit_commission_usd) STORED,

  -- FUNDING_FEE income recorded by Binance while THIS position was open, in the
  -- sign Binance returns: negative when the customer paid funding, positive
  -- when they received it. Attribution rule, applied by the synchroniser:
  -- a funding event belongs to the one trade whose [entry_time, exit_time]
  -- window contains it. Positions are flat-to-flat, so those windows never
  -- overlap and no event can be counted against two trades.
  funding_usd numeric,
  funding_event_count integer NOT NULL DEFAULT 0 CHECK (funding_event_count >= 0),

  -- Also generated. The customer-facing bottom line is derived from its parts
  -- and cannot be written to a value inconsistent with them.
  net_pnl_usd numeric GENERATED ALWAYS AS
    (gross_pnl_usd - entry_commission_usd - exit_commission_usd + funding_usd) STORED,

  -- When exact accounting cannot be established from Binance's own numbers the
  -- row is still recorded, with the money columns NULL, so the customer is told
  -- "we cannot show this yet" instead of being shown an estimate.
  accounting_status text NOT NULL DEFAULT 'COMPLETE'
    CHECK (accounting_status IN ('COMPLETE', 'INCOMPLETE')),
  incomplete_reason text CHECK (incomplete_reason IS NULL OR length(incomplete_reason) <= 300),

  source text NOT NULL DEFAULT 'BINANCE' CHECK (source = 'BINANCE'),
  synced_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT executed_trades_exit_not_before_entry CHECK (exit_time >= entry_time),

  -- A COMPLETE row is fully priced or it is not COMPLETE. This is what stops a
  -- partially-known trade from rendering as "commission $0.00".
  CONSTRAINT executed_trades_complete_is_fully_priced CHECK (
    accounting_status <> 'COMPLETE' OR (
      qty IS NOT NULL
      AND entry_avg_price IS NOT NULL
      AND exit_avg_price IS NOT NULL
      AND entry_fill_count > 0
      AND exit_fill_count > 0
      AND exit_order_count > 0
      AND gross_pnl_usd IS NOT NULL
      AND entry_commission_usd IS NOT NULL
      AND exit_commission_usd IS NOT NULL
      AND funding_usd IS NOT NULL
      AND incomplete_reason IS NULL
    )
  ),
  CONSTRAINT executed_trades_incomplete_names_its_reason CHECK (
    accounting_status <> 'INCOMPLETE' OR incomplete_reason IS NOT NULL
  ),

  -- IDEMPOTENCY. One closed Binance trade is one row, forever. Re-running the
  -- synchroniser over the same window upserts onto this key and can only
  -- restate the same trade, never duplicate its P&L into the customer's totals.
  CONSTRAINT executed_trades_one_row_per_closed_trade
    UNIQUE (user_id, close_binance_order_id)
);

CREATE INDEX executed_trades_user_exit_idx
  ON public.executed_trades(user_id, exit_time DESC);

-- updated_at is maintained by the database rather than trusted from the caller.
CREATE OR REPLACE FUNCTION public.touch_executed_trades_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$fn$;

REVOKE ALL ON FUNCTION public.touch_executed_trades_updated_at() FROM PUBLIC, anon, authenticated;

CREATE TRIGGER executed_trades_touch_updated_at
  BEFORE UPDATE ON public.executed_trades
  FOR EACH ROW EXECUTE FUNCTION public.touch_executed_trades_updated_at();

-- The customer may read. Only the service role may write: an authenticated
-- session has no INSERT/UPDATE/DELETE grant and no policy permitting one, so a
-- client cannot author or edit their own "real Binance" P&L. anon gets nothing.
GRANT SELECT ON public.executed_trades TO authenticated;
GRANT ALL ON public.executed_trades TO service_role;

ALTER TABLE public.executed_trades ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own executed_trades select"
  ON public.executed_trades
  FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

COMMENT ON TABLE public.executed_trades IS
  'Real Binance completed-trade accounting: realised P&L, exact per-fill commission, funding and net P&L. Customer-readable, service-role-writable, never read by trading logic.';
COMMENT ON COLUMN public.executed_trades.gross_pnl_usd IS
  'Sum of Binance realizedPnl across the closing order fills. Before commission and funding.';
COMMENT ON COLUMN public.executed_trades.commission_usd IS
  'Generated: entry_commission_usd + exit_commission_usd. Never a fee-rate estimate.';
COMMENT ON COLUMN public.executed_trades.funding_usd IS
  'Binance FUNDING_FEE income inside [entry_time, exit_time], Binance sign convention (negative = paid).';
COMMENT ON COLUMN public.executed_trades.net_pnl_usd IS
  'Generated: gross_pnl_usd - commission_usd + funding_usd.';
