
DROP VIEW IF EXISTS public.binance_keys_safe;
CREATE VIEW public.binance_keys_safe
WITH (security_invoker = false)
AS SELECT user_id, api_key_last4, permissions_note, created_at, updated_at
   FROM public.binance_keys
   WHERE user_id = auth.uid();
GRANT SELECT ON public.binance_keys_safe TO authenticated;
