
-- Restrict diagnostics, signals, worker_logs to admins
DROP POLICY IF EXISTS "authenticated read diagnostics" ON public.diagnostics;
CREATE POLICY "admins read diagnostics" ON public.diagnostics FOR SELECT TO authenticated USING (public.is_admin());

DROP POLICY IF EXISTS "authenticated read signals" ON public.signals;
CREATE POLICY "admins read signals" ON public.signals FOR SELECT TO authenticated USING (public.is_admin());

DROP POLICY IF EXISTS "authenticated read logs" ON public.worker_logs;
CREATE POLICY "admins read worker logs" ON public.worker_logs FOR SELECT TO authenticated USING (public.is_admin());

-- Revoke EXECUTE on trigger-only SECURITY DEFINER function from clients
REVOKE EXECUTE ON FUNCTION public.handle_new_user_role() FROM PUBLIC, anon, authenticated;
