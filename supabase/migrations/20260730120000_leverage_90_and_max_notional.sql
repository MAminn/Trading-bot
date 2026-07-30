-- Leverage ceiling raised to 90x and an explicit per-order notional cap.
-- Allocation stays the single source of truth in the UI; leverage is derived.

ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_leverage_check;
ALTER TABLE public.engine_config
  ADD CONSTRAINT engine_config_leverage_check
  CHECK (leverage >= 1 AND leverage <= 90);

-- 500 intentionally matches executor HARD_CAP_USD for the first soak.
-- A lower default here would bind first and silently shrink order size.
ALTER TABLE public.engine_config
  ADD COLUMN IF NOT EXISTS max_notional_usd numeric NOT NULL DEFAULT 500;
ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_max_notional_check;
ALTER TABLE public.engine_config
  ADD CONSTRAINT engine_config_max_notional_check
  CHECK (max_notional_usd > 0 AND max_notional_usd <= 100000);
