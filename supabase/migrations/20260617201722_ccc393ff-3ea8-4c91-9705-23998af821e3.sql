CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net;

-- Remove any prior schedule with same name
DO $$ BEGIN
  PERFORM cron.unschedule('engine-builtin-tick');
EXCEPTION WHEN OTHERS THEN NULL; END $$;

SELECT cron.schedule(
  'engine-builtin-tick',
  '* * * * *',
  $$
  SELECT net.http_post(
    url := 'https://project--e6e62973-7761-48f9-a0d1-e6b08bd22dff.lovable.app/api/public/engine/demo-tick',
    headers := '{"Content-Type": "application/json", "apikey": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImlmdnZqeW1kYmhmeXhxanR2ZHlmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA5OTE2NTksImV4cCI6MjA5NjU2NzY1OX0.BcjmDsDYD1m8dosPUAIjraRIRNlenJrXrqpEXTAZEDY"}'::jsonb,
    body := '{}'::jsonb
  ) AS request_id;
  $$
);