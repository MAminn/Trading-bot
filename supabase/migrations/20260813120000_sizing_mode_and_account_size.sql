-- Two mutually exclusive sizing modes. 'allocation' is the backward-compatible default.
ALTER TABLE public.engine_config
  ADD COLUMN IF NOT EXISTS sizing_mode text NOT NULL DEFAULT 'allocation';

ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_sizing_mode_check;
ALTER TABLE public.engine_config
  ADD CONSTRAINT engine_config_sizing_mode_check
  CHECK (sizing_mode IN ('allocation','full_capital'));

-- Account size becomes first-class. Today the UI reconstructs it by division,
-- which is lossy; full_capital mode sizes off it directly.
ALTER TABLE public.engine_config
  ADD COLUMN IF NOT EXISTS account_size_usd numeric;

UPDATE public.engine_config
SET account_size_usd = CASE
      WHEN capital_allocation_pct > 0
        THEN capital_usd / (capital_allocation_pct / 100.0)
      ELSE capital_usd
    END
WHERE account_size_usd IS NULL;

ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_account_size_check;
ALTER TABLE public.engine_config
  ADD CONSTRAINT engine_config_account_size_check
  CHECK (account_size_usd IS NULL OR account_size_usd > 0);

-- full_capital may never exist without a usable account size.
ALTER TABLE public.engine_config
  DROP CONSTRAINT IF EXISTS engine_config_full_capital_requires_account_size;
ALTER TABLE public.engine_config
  ADD CONSTRAINT engine_config_full_capital_requires_account_size
  CHECK (sizing_mode <> 'full_capital'
         OR (account_size_usd IS NOT NULL AND account_size_usd > 0));
