ALTER TABLE public.user_signals ADD COLUMN IF NOT EXISTS trade_id text;
CREATE INDEX IF NOT EXISTS user_signals_user_trade_idx ON public.user_signals(user_id, trade_id) WHERE trade_id IS NOT NULL;