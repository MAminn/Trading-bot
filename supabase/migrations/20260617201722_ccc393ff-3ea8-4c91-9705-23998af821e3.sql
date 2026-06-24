-- Hostinger deployment: old Lovable demo cron disabled.
-- This migration previously scheduled `engine-builtin-tick` to POST to a
-- hard-coded *.lovable.app demo-tick URL. On the Hostinger VPS the Python
-- worker drives the engine directly, so no external cron is needed.
-- This is now a safe no-op cleanup that only unschedules the old job if it
-- exists and never calls any external URL.
DO $$
BEGIN
  PERFORM cron.unschedule('engine-builtin-tick');
EXCEPTION WHEN OTHERS THEN NULL;
END $$;