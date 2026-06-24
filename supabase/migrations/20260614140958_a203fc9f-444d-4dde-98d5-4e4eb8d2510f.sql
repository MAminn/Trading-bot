
-- 1. binance_keys: add owner-scoped RLS policies (defense-in-depth; app uses SECURITY DEFINER fns)
CREATE POLICY "Users can view own binance keys metadata"
  ON public.binance_keys FOR SELECT
  TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own binance keys"
  ON public.binance_keys FOR INSERT
  TO authenticated
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own binance keys"
  ON public.binance_keys FOR UPDATE
  TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can delete own binance keys"
  ON public.binance_keys FOR DELETE
  TO authenticated
  USING (auth.uid() = user_id);

-- 2. Pin search_path on touch_updated_at
CREATE OR REPLACE FUNCTION public.touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO 'public'
AS $function$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$function$;

-- 3. Revoke anon/public EXECUTE on SECURITY DEFINER functions; grant only where needed
REVOKE EXECUTE ON FUNCTION public.save_binance_keys(text, text, text) FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.delete_binance_keys() FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.get_my_binance_key_info() FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.decrypt_binance_keys_for(uuid) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.has_role(uuid, public.app_role) FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.is_admin() FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.handle_new_user_engine() FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.handle_new_user_role() FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.save_binance_keys(text, text, text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.delete_binance_keys() TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_my_binance_key_info() TO authenticated;
GRANT EXECUTE ON FUNCTION public.has_role(uuid, public.app_role) TO authenticated;
GRANT EXECUTE ON FUNCTION public.is_admin() TO authenticated;
GRANT EXECUTE ON FUNCTION public.decrypt_binance_keys_for(uuid) TO service_role;
