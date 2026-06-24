
-- Roles
CREATE TYPE public.app_role AS ENUM ('admin', 'viewer');

CREATE TABLE public.user_roles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  role public.app_role NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(user_id, role)
);
GRANT SELECT ON public.user_roles TO authenticated;
GRANT ALL ON public.user_roles TO service_role;
ALTER TABLE public.user_roles ENABLE ROW LEVEL SECURITY;
CREATE POLICY "users read own roles" ON public.user_roles FOR SELECT TO authenticated USING (user_id = auth.uid());

CREATE OR REPLACE FUNCTION public.has_role(_user_id uuid, _role public.app_role)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (SELECT 1 FROM public.user_roles WHERE user_id = _user_id AND role = _role)
$$;

CREATE OR REPLACE FUNCTION public.is_admin()
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT public.has_role(auth.uid(), 'admin')
$$;

-- Enums
CREATE TYPE public.control_flag AS ENUM ('RUNNING', 'PAUSED', 'STOPPED');
CREATE TYPE public.worker_state AS ENUM ('OK', 'DEGRADED', 'HALTED', 'UNKNOWN');
CREATE TYPE public.audit_status AS ENUM ('PASS', 'WARN', 'FAIL', 'UNKNOWN');
CREATE TYPE public.trade_side AS ENUM ('LONG', 'SHORT');
CREATE TYPE public.position_state AS ENUM ('OPEN', 'CLOSED');

-- model_versions
CREATE TABLE public.model_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  version_label text NOT NULL UNIQUE,
  storage_key text NOT NULL,
  fingerprint jsonb NOT NULL DEFAULT '{}'::jsonb,
  training_window text,
  is_active boolean NOT NULL DEFAULT false,
  status text NOT NULL DEFAULT 'VALID',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX model_versions_one_active ON public.model_versions ((is_active)) WHERE is_active;
GRANT SELECT ON public.model_versions TO authenticated;
GRANT ALL ON public.model_versions TO service_role;
ALTER TABLE public.model_versions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "authenticated read models" ON public.model_versions FOR SELECT TO authenticated USING (true);

-- system_status: single-row table
CREATE TABLE public.system_status (
  id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  control_flag public.control_flag NOT NULL DEFAULT 'STOPPED',
  worker_state public.worker_state NOT NULL DEFAULT 'UNKNOWN',
  last_heartbeat timestamptz,
  last_bar_ts timestamptz,
  active_version text,
  audit_status public.audit_status NOT NULL DEFAULT 'UNKNOWN',
  updated_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT ON public.system_status TO authenticated;
GRANT UPDATE (control_flag, updated_at) ON public.system_status TO authenticated;
GRANT ALL ON public.system_status TO service_role;
ALTER TABLE public.system_status ENABLE ROW LEVEL SECURITY;
CREATE POLICY "authenticated read status" ON public.system_status FOR SELECT TO authenticated USING (true);
CREATE POLICY "admin update control flag" ON public.system_status FOR UPDATE TO authenticated USING (public.is_admin()) WITH CHECK (public.is_admin());

-- signals
CREATE TABLE public.signals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_version text NOT NULL,
  bar_timestamp timestamptz NOT NULL,
  side public.trade_side NOT NULL,
  setup text,
  ml_probability numeric,
  ml_threshold numeric,
  vetoed boolean NOT NULL DEFAULT false,
  veto_reason text,
  emitted boolean NOT NULL DEFAULT false,
  price numeric,
  diagnostics jsonb DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(model_version, bar_timestamp, side)
);
CREATE INDEX signals_bar_ts_idx ON public.signals (bar_timestamp DESC);
GRANT SELECT ON public.signals TO authenticated;
GRANT ALL ON public.signals TO service_role;
ALTER TABLE public.signals ENABLE ROW LEVEL SECURITY;
CREATE POLICY "authenticated read signals" ON public.signals FOR SELECT TO authenticated USING (true);

-- trades
CREATE TABLE public.trades (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_version text NOT NULL,
  side public.trade_side NOT NULL,
  entry_ts timestamptz NOT NULL,
  exit_ts timestamptz,
  entry_price numeric NOT NULL,
  exit_price numeric,
  quantity numeric NOT NULL DEFAULT 1,
  pnl numeric,
  pnl_pct numeric,
  exit_reason text,
  setup text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(model_version, entry_ts, side)
);
CREATE INDEX trades_entry_ts_idx ON public.trades (entry_ts DESC);
GRANT SELECT ON public.trades TO authenticated;
GRANT ALL ON public.trades TO service_role;
ALTER TABLE public.trades ENABLE ROW LEVEL SECURITY;
CREATE POLICY "authenticated read trades" ON public.trades FOR SELECT TO authenticated USING (true);

-- positions
CREATE TABLE public.positions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_version text NOT NULL,
  side public.trade_side NOT NULL,
  state public.position_state NOT NULL DEFAULT 'OPEN',
  entry_ts timestamptz NOT NULL,
  entry_price numeric NOT NULL,
  quantity numeric NOT NULL DEFAULT 1,
  stop_loss numeric,
  take_profit numeric,
  unrealized_pnl numeric,
  last_price numeric,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX positions_state_idx ON public.positions (state);
GRANT SELECT ON public.positions TO authenticated;
GRANT ALL ON public.positions TO service_role;
ALTER TABLE public.positions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "authenticated read positions" ON public.positions FOR SELECT TO authenticated USING (true);

-- diagnostics
CREATE TABLE public.diagnostics (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  bar_timestamp timestamptz NOT NULL,
  model_version text NOT NULL,
  audit_status public.audit_status NOT NULL,
  fingerprint_match boolean,
  parity_ok boolean,
  details jsonb DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX diagnostics_ts_idx ON public.diagnostics (bar_timestamp DESC);
GRANT SELECT ON public.diagnostics TO authenticated;
GRANT ALL ON public.diagnostics TO service_role;
ALTER TABLE public.diagnostics ENABLE ROW LEVEL SECURITY;
CREATE POLICY "authenticated read diagnostics" ON public.diagnostics FOR SELECT TO authenticated USING (true);

-- worker_logs (sanitized)
CREATE TABLE public.worker_logs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  level text NOT NULL DEFAULT 'INFO',
  message text NOT NULL,
  context jsonb DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX worker_logs_created_idx ON public.worker_logs (created_at DESC);
GRANT SELECT ON public.worker_logs TO authenticated;
GRANT ALL ON public.worker_logs TO service_role;
ALTER TABLE public.worker_logs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "authenticated read logs" ON public.worker_logs FOR SELECT TO authenticated USING (true);

-- admin_audit
CREATE TABLE public.admin_audit (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_id uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  actor_email text,
  action text NOT NULL,
  payload jsonb DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX admin_audit_created_idx ON public.admin_audit (created_at DESC);
GRANT SELECT ON public.admin_audit TO authenticated;
GRANT INSERT ON public.admin_audit TO authenticated;
GRANT ALL ON public.admin_audit TO service_role;
ALTER TABLE public.admin_audit ENABLE ROW LEVEL SECURITY;
CREATE POLICY "admin read audit" ON public.admin_audit FOR SELECT TO authenticated USING (public.is_admin());
CREATE POLICY "admin insert audit" ON public.admin_audit FOR INSERT TO authenticated WITH CHECK (public.is_admin() AND actor_id = auth.uid());

-- Seed system_status single row
INSERT INTO public.system_status (id, control_flag, worker_state, audit_status)
VALUES (1, 'STOPPED', 'UNKNOWN', 'UNKNOWN')
ON CONFLICT (id) DO NOTHING;

-- Auto-grant viewer role on signup; first user becomes admin
CREATE OR REPLACE FUNCTION public.handle_new_user_role()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  INSERT INTO public.user_roles (user_id, role) VALUES (NEW.id, 'viewer') ON CONFLICT DO NOTHING;
  IF NOT EXISTS (SELECT 1 FROM public.user_roles WHERE role = 'admin') THEN
    INSERT INTO public.user_roles (user_id, role) VALUES (NEW.id, 'admin') ON CONFLICT DO NOTHING;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER on_auth_user_created_role
AFTER INSERT ON auth.users
FOR EACH ROW EXECUTE FUNCTION public.handle_new_user_role();
