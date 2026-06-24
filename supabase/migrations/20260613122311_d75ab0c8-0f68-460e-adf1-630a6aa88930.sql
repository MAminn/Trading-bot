
CREATE POLICY "admin update model versions" ON public.model_versions
  FOR UPDATE TO authenticated
  USING (public.is_admin()) WITH CHECK (public.is_admin());
