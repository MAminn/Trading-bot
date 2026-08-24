-- =========================================================
-- ONE sizing model, and only one
-- =========================================================
-- Position size is now, everywhere and without exception:
--
--     target notional = <live Binance USD-M totalWalletBalance>
--                     x capital_allocation_pct / 100
--                     x leverage
--
-- The capital base is read from the authenticated user's OWN Binance Futures
-- account by the executor on every sizing decision. It is deliberately NOT a
-- column: a stored or hand-entered balance is exactly what this migration
-- exists to make unrepresentable.
--
-- capital_allocation_pct and leverage are INDEPENDENT. Nothing derives one from
-- the other, nothing constrains them as a pair, and moving one never moves the
-- other. The old allocation->leverage mapping is gone.
--
-- Everything belonging to the removed full-capital system is dropped rather
-- than deprecated. A column left behind is a behaviour that can come back.

-- --------------------------------------------------------
-- 1. Snap existing values onto the permitted discrete sets
-- --------------------------------------------------------
-- Done BEFORE the CHECKs are added, or an existing row would make the
-- constraint unaddable. Rounding is always DOWN to the nearest permitted value:
-- a legacy row that cannot be represented exactly must land on LESS exposure,
-- never more. Users below the smallest step land on it.

-- Permitted allocations: 1, then exact 5% steps — 5, 10, 15 ... 100.
-- 1% is a deliberate special first step below the regular grid.
--
-- Snap-down rule, in one expression:
--   >= 5  -> floor(pct / 5) * 5   (7 -> 5, 12 -> 10, 17 -> 15, 99 -> 95, 100 -> 100)
--   < 5   -> 1                    (4 -> 1, 1 -> 1, and anything smaller)
UPDATE public.engine_config
SET capital_allocation_pct = CASE
      WHEN capital_allocation_pct >= 100 THEN 100
      WHEN capital_allocation_pct >= 5 THEN floor(capital_allocation_pct / 5) * 5
      ELSE 1
    END
WHERE capital_allocation_pct IS DISTINCT FROM CASE
      WHEN capital_allocation_pct >= 100 THEN 100
      WHEN capital_allocation_pct >= 5 THEN floor(capital_allocation_pct / 5) * 5
      ELSE 1
    END;

-- Permitted leverage: 1, 10, 20, ... 90.
UPDATE public.engine_config
SET leverage = CASE
      WHEN leverage >= 90 THEN 90
      WHEN leverage >= 10 THEN floor(leverage / 10) * 10
      ELSE 1
    END
WHERE leverage IS DISTINCT FROM CASE
      WHEN leverage >= 90 THEN 90
      WHEN leverage >= 10 THEN floor(leverage / 10) * 10
      ELSE 1
    END;

ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_alloc_check;
ALTER TABLE public.engine_config
  ADD CONSTRAINT engine_config_alloc_check
  CHECK (capital_allocation_pct IN (
    1,
    5,10,15,20,25,30,35,40,45,50,
    55,60,65,70,75,80,85,90,95,100
  ));

ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_leverage_check;
ALTER TABLE public.engine_config
  ADD CONSTRAINT engine_config_leverage_check
  CHECK (leverage IN (1,10,20,30,40,50,60,70,80,90));

-- --------------------------------------------------------
-- 2. Remove the full-capital system entirely
-- --------------------------------------------------------
-- Constraints first: they reference the columns being dropped.
ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_sizing_mode_check;
ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_account_size_check;
ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_full_capital_requires_account_size;
ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_live_full_capital_requires_flag;
ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_max_notional_check;

-- The sizing-mode selector. There is one sizing model, so there is nothing to
-- select between.
ALTER TABLE public.engine_config DROP COLUMN IF EXISTS sizing_mode;

-- The manual "Account size (USD)" figure. Replaced by the live wallet reading.
ALTER TABLE public.engine_config DROP COLUMN IF EXISTS account_size_usd;

-- The full-capital dual-consent flag (the other half lived in the executor's
-- LIVE_ALLOW_FULL_CAPITAL env var, also removed).
ALTER TABLE public.engine_config DROP COLUMN IF EXISTS live_allow_full_capital;

-- The config-driven notional cap. It defaulted to 500 and was the twin of the
-- executor's HARD_CAP_USD: under the new formula it would silently shrink a
-- correctly-sized order (a $300 wallet at 20% and 30x asks for $1,800) back to
-- the old soak ceiling. Binance's own leverage-bracket ceiling is the only
-- notional bound that survives anywhere.
ALTER TABLE public.engine_config DROP COLUMN IF EXISTS max_notional_usd;

-- --------------------------------------------------------
-- 3. Remove the per-order dollar cap
-- --------------------------------------------------------
-- live_order_cap_usd was an absolute per-order notional ceiling applied AFTER
-- the sizing formula. Its default was 500 — the twin of the removed
-- HARD_CAP_USD — so under the new model it would have silently reduced a $300
-- wallet at 20% and 30x from the configured $1,800 to $500. A control that
-- changes the size of an order without changing the configuration that produced
-- it is a second sizing model, not a safety control, so it is removed rather
-- than raised.
--
-- The controls that stop a live executor are unchanged, and every one of them is
-- binary — it halts trading rather than quietly resizing it:
--   * engine_config.execution_mode   (OFF / LIVE_READ / LIVE_TRADE)
--   * engine_config.mode             (auto_execute_enabled)
--   * engine_config.is_running       (the Stop switch)
--   * the executor host's EXECUTION_MODE and LIVE_TRADING_ACK environment
ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_live_order_cap_check;
ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_live_trade_requires_cap;
ALTER TABLE public.engine_config DROP COLUMN IF EXISTS live_order_cap_usd;

-- The telemetry columns that mirrored it. Nothing reports them any more, and a
-- dashboard tile reading "Effective order cap: $500" would now be false.
ALTER TABLE public.executor_status DROP COLUMN IF EXISTS live_order_cap_usd;
ALTER TABLE public.executor_status DROP COLUMN IF EXISTS live_order_cap_env_max;

-- --------------------------------------------------------
-- 4. Column privileges
-- --------------------------------------------------------
-- Re-granted because migration 20260817131000's list names columns that no
-- longer exist, and because capital_usd must stop being user-writable.
--
-- capital_usd is retained ONLY because it is NOT NULL and the reporting pages
-- still scale strategy percentage returns by it. It is NOT an input to order
-- sizing anywhere, and it is NOT the client's Binance balance. A known separate
-- defect is that "net_pnl_rate x capital_usd" does not equal realised Binance
-- P&L; that is tracked as its own change and is deliberately not addressed
-- here. Removing the grant costs nothing and closes the last path by which a
-- hand-entered "account size" could reach a sizing calculation.
--
-- As in 20260817131000: the table-level grant must be dropped and re-granted as
-- an explicit column list, because REVOKE UPDATE (col) does not override a
-- table-level GRANT UPDATE.
REVOKE UPDATE ON public.engine_config FROM authenticated;

GRANT UPDATE (
  capital_allocation_pct,
  leverage,
  max_daily_loss_usd,
  max_position_size_usd,
  is_running,
  demo_mode,
  updated_at
) ON public.engine_config TO authenticated;

COMMENT ON COLUMN public.engine_config.capital_allocation_pct IS
  'Percentage of the user''s live Binance USD-M totalWalletBalance committed as margin. One of 1, or 5-100 in 5%% steps. Independent of leverage.';
COMMENT ON COLUMN public.engine_config.leverage IS
  'Leverage multiplier applied to the allocated margin. Independent of capital_allocation_pct.';
COMMENT ON COLUMN public.engine_config.capital_usd IS
  'LEGACY. Retained only to scale strategy P&L on the reporting pages. NOT an input to live order sizing and no longer writable by the app.';
