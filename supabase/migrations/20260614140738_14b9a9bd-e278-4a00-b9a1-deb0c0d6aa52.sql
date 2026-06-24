
DROP VIEW IF EXISTS public.binance_keys_safe;

CREATE OR REPLACE FUNCTION public.get_my_binance_key_info()
RETURNS TABLE(api_key_last4 text, permissions_note text, created_at timestamptz, updated_at timestamptz)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT api_key_last4, permissions_note, created_at, updated_at
  FROM public.binance_keys
  WHERE user_id = auth.uid();
$$;
REVOKE ALL ON FUNCTION public.get_my_binance_key_info() FROM public;
GRANT EXECUTE ON FUNCTION public.get_my_binance_key_info() TO authenticated;
