
-- Remove all seed/demo data so the UI only shows what the live worker emits
DELETE FROM public.signals;
DELETE FROM public.trades;
DELETE FROM public.positions;
DELETE FROM public.diagnostics;
DELETE FROM public.model_versions;

-- Reset engine status to a true "no worker yet" state
UPDATE public.system_status
SET control_flag = 'STOPPED',
    worker_state = 'UNKNOWN',
    last_heartbeat = NULL,
    last_bar_ts = NULL,
    active_version = NULL,
    audit_status = 'UNKNOWN',
    updated_at = now()
WHERE id = 1;

-- Security fix: restrict model_versions reads to admins only
DROP POLICY IF EXISTS "Authenticated users can read model_versions" ON public.model_versions;
DROP POLICY IF EXISTS "model_versions readable by authenticated" ON public.model_versions;
DROP POLICY IF EXISTS "Anyone authenticated can read model_versions" ON public.model_versions;
DROP POLICY IF EXISTS "model_versions read" ON public.model_versions;

CREATE POLICY "Admins can read model_versions"
  ON public.model_versions FOR SELECT
  TO authenticated
  USING (public.is_admin());
