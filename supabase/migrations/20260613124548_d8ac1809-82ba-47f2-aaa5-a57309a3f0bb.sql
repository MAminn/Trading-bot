
-- Backfill: give every existing user viewer + admin (so current accounts can manage the engine)
INSERT INTO public.user_roles (user_id, role)
SELECT id, 'viewer'::public.app_role FROM auth.users
ON CONFLICT DO NOTHING;

INSERT INTO public.user_roles (user_id, role)
SELECT id, 'admin'::public.app_role FROM auth.users
ON CONFLICT DO NOTHING;

-- Wire up the new-user trigger so future signups get viewer (and admin if none exists yet)
DROP TRIGGER IF EXISTS on_auth_user_created_role ON auth.users;
CREATE TRIGGER on_auth_user_created_role
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user_role();
