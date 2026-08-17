-- =========================================================
-- Web-controlled live execution: COLUMNS AND CONSTRAINTS ONLY
-- =========================================================
-- Nothing reads these yet. The executor's live capability still comes
-- exclusively from its own environment, and no placement, gate or sizing path
-- consults any column added here. This migration exists so the schema can land
-- ahead of the executor, and so an executor running the current build is
-- entirely unaffected by it.
--
-- Every default is fail-closed: an existing row lands on OFF, a zero cap, and
-- no full-capital permission, regardless of what its .env-driven executor is
-- currently doing. Turning the DB into a live control surface is a later,
-- deliberate step — never a side effect of applying a migration.

-- What the user asks the executor to do. Deliberately narrower than the
-- executor's own mode set: TESTNET_* is a property of the host's environment,
-- not something a web UI should be able to select.
ALTER TABLE public.engine_config
  ADD COLUMN IF NOT EXISTS execution_mode text NOT NULL DEFAULT 'OFF';
ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_execution_mode_check;
ALTER TABLE public.engine_config
  ADD CONSTRAINT engine_config_execution_mode_check
  CHECK (execution_mode IN ('OFF','LIVE_READ','LIVE_TRADE'));

-- Absolute per-order notional ceiling requested from the web. 500 is the same
-- number as the executor's LIVE_ORDER_CAP_MAX_USD, HARD_CAP_USD and
-- RiskGuard.ABSOLUTE_MAX_NOTIONAL_USD: the DB must not be able to express a
-- cap the executor would refuse to honour. 0 means "no live orders".
ALTER TABLE public.engine_config
  ADD COLUMN IF NOT EXISTS live_order_cap_usd numeric NOT NULL DEFAULT 0;
ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_live_order_cap_check;
ALTER TABLE public.engine_config
  ADD CONSTRAINT engine_config_live_order_cap_check
  CHECK (live_order_cap_usd >= 0 AND live_order_cap_usd <= 500);

-- Explicit permission for the uncapped sizing path on a live executor. Off by
-- default, and on its own still insufficient: the executor additionally
-- requires LIVE_ALLOW_FULL_CAPITAL=1 in its own environment.
ALTER TABLE public.engine_config
  ADD COLUMN IF NOT EXISTS live_allow_full_capital boolean NOT NULL DEFAULT false;

-- Auto-execute is derived, never stored independently. `mode` already exists
-- and is already constrained to signal_only/auto; a second free boolean could
-- disagree with it, and two sources of truth for "may this place orders"
-- is precisely the ambiguity worth designing out.
ALTER TABLE public.engine_config
  ADD COLUMN IF NOT EXISTS auto_execute_enabled boolean
  GENERATED ALWAYS AS (mode = 'auto') STORED;

-- --------------------------------------------------------
-- Cross-field constraints: illegal combinations are unrepresentable, so a
-- validator bug or a direct PostgREST write cannot produce one.
-- --------------------------------------------------------

-- ETHUSDT's exchange minimum notional is 20 USDT. After rounding down to the
-- 0.001 step size a cap below ~25 produces orders that are rejected as "below
-- min notional" — a live mode that silently never trades. Refuse the state.
ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_live_trade_requires_cap;
ALTER TABLE public.engine_config
  ADD CONSTRAINT engine_config_live_trade_requires_cap
  CHECK (execution_mode <> 'LIVE_TRADE' OR live_order_cap_usd >= 25);

-- full_capital has no internal notional ceiling. On a live-trading config it
-- requires the explicit opt-in, never the mere absence of an objection.
ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_live_full_capital_requires_flag;
ALTER TABLE public.engine_config
  ADD CONSTRAINT engine_config_live_full_capital_requires_flag
  CHECK (execution_mode <> 'LIVE_TRADE'
         OR sizing_mode <> 'full_capital'
         OR live_allow_full_capital);

-- Demo mode fabricates signals and trades. A live executor consuming them
-- would place real orders against invented data, so the two may never coexist.
ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_demo_not_live;
ALTER TABLE public.engine_config
  ADD CONSTRAINT engine_config_demo_not_live
  CHECK (NOT demo_mode OR execution_mode = 'OFF');

-- Every new column is NOT NULL with a default, which matters for the CHECKs
-- above: a constraint evaluating to NULL passes, so a nullable control column
-- would be a silently open gate.
