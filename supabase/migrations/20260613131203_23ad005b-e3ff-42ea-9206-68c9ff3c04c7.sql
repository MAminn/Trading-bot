
DROP POLICY IF EXISTS "deny insert user_roles" ON public.user_roles;
DROP POLICY IF EXISTS "deny update user_roles" ON public.user_roles;
DROP POLICY IF EXISTS "deny delete user_roles" ON public.user_roles;

CREATE POLICY "deny insert user_roles"
  ON public.user_roles AS RESTRICTIVE FOR INSERT TO authenticated, anon
  WITH CHECK (false);

CREATE POLICY "deny update user_roles"
  ON public.user_roles AS RESTRICTIVE FOR UPDATE TO authenticated, anon
  USING (false) WITH CHECK (false);

CREATE POLICY "deny delete user_roles"
  ON public.user_roles AS RESTRICTIVE FOR DELETE TO authenticated, anon
  USING (false);
