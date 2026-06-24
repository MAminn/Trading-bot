// Admin-only server functions: list users with engine performance stats.
import { createServerFn } from "@tanstack/react-start";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";

export interface AdminUserRow {
  user_id: string;
  email: string | null;
  full_name: string | null;
  created_at: string | null;
  last_sign_in_at: string | null;
  is_running: boolean;
  demo_mode: boolean;
  capital_usd: number;
  engine_status: string | null;
  last_heartbeat: string | null;
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  net_pnl_rate: number; // sum of fractional returns
  net_pnl_usd: number;  // sum scaled by each user's capital_usd
  open_positions: number;
  signals_24h: number;
}

export interface AdminOverview {
  totals: {
    users: number;
    active_engines: number;
    total_trades: number;
    total_wins: number;
    total_losses: number;
    overall_win_rate: number;
    total_net_pnl_usd: number;
    total_open_positions: number;
    total_signals_24h: number;
  };
  users: AdminUserRow[];
}

export const getAdminOverview = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }): Promise<AdminOverview> => {
    // Verify caller is admin
    const { data: isAdmin, error: roleErr } = await context.supabase.rpc("has_role", {
      _user_id: context.userId,
      _role: "admin",
    });
    if (roleErr) throw new Error(roleErr.message);
    if (!isAdmin) throw new Error("Forbidden: admin role required");

    const { supabaseAdmin } = await import("@/integrations/supabase/client.server");

    // List auth users (paginate up to 1000)
    const { data: usersPage, error: usersErr } = await supabaseAdmin.auth.admin.listUsers({
      page: 1,
      perPage: 1000,
    });
    if (usersErr) throw new Error(usersErr.message);
    const users = usersPage.users;

    // Pull supporting tables in parallel
    const since24h = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
    const [configsRes, statusRes, tradesRes, openRes, signalsRes] = await Promise.all([
      supabaseAdmin.from("engine_config").select("user_id,is_running,demo_mode,capital_usd"),
      supabaseAdmin.from("engine_status").select("user_id,status,last_heartbeat"),
      supabaseAdmin.from("user_trades").select("user_id,net_pnl_rate").not("exit_t", "is", null),
      supabaseAdmin.from("open_positions").select("user_id"),
      supabaseAdmin.from("user_signals").select("user_id,created_at").gte("created_at", since24h),
    ]);

    const configs = new Map<string, { is_running: boolean; demo_mode: boolean; capital_usd: number }>();
    (configsRes.data ?? []).forEach((r: any) => configs.set(r.user_id, {
      is_running: !!r.is_running,
      demo_mode: !!r.demo_mode,
      capital_usd: Number(r.capital_usd ?? 10000),
    }));

    const statuses = new Map<string, { status: string | null; last_heartbeat: string | null }>();
    (statusRes.data ?? []).forEach((r: any) => statuses.set(r.user_id, {
      status: r.status ?? null,
      last_heartbeat: r.last_heartbeat ?? null,
    }));

    const tradeAgg = new Map<string, { total: number; wins: number; losses: number; pnlRate: number }>();
    (tradesRes.data ?? []).forEach((r: any) => {
      const agg = tradeAgg.get(r.user_id) ?? { total: 0, wins: 0, losses: 0, pnlRate: 0 };
      const pnl = Number(r.net_pnl_rate ?? 0);
      agg.total += 1;
      if (pnl > 0) agg.wins += 1;
      else if (pnl < 0) agg.losses += 1;
      agg.pnlRate += pnl;
      tradeAgg.set(r.user_id, agg);
    });

    const openCount = new Map<string, number>();
    (openRes.data ?? []).forEach((r: any) => openCount.set(r.user_id, (openCount.get(r.user_id) ?? 0) + 1));

    const sigCount = new Map<string, number>();
    (signalsRes.data ?? []).forEach((r: any) => sigCount.set(r.user_id, (sigCount.get(r.user_id) ?? 0) + 1));

    const rows: AdminUserRow[] = users.map((u) => {
      const cfg = configs.get(u.id) ?? { is_running: false, demo_mode: false, capital_usd: 10000 };
      const st = statuses.get(u.id) ?? { status: null, last_heartbeat: null };
      const t = tradeAgg.get(u.id) ?? { total: 0, wins: 0, losses: 0, pnlRate: 0 };
      const meta = (u.user_metadata ?? {}) as Record<string, string>;
      return {
        user_id: u.id,
        email: u.email ?? null,
        full_name: meta.full_name ?? null,
        created_at: u.created_at ?? null,
        last_sign_in_at: u.last_sign_in_at ?? null,
        is_running: cfg.is_running,
        demo_mode: cfg.demo_mode,
        capital_usd: cfg.capital_usd,
        engine_status: st.status,
        last_heartbeat: st.last_heartbeat,
        total_trades: t.total,
        wins: t.wins,
        losses: t.losses,
        win_rate: t.total > 0 ? (t.wins / t.total) * 100 : 0,
        net_pnl_rate: t.pnlRate,
        net_pnl_usd: t.pnlRate * cfg.capital_usd,
        open_positions: openCount.get(u.id) ?? 0,
        signals_24h: sigCount.get(u.id) ?? 0,
      };
    });

    const totals = rows.reduce(
      (a, r) => {
        a.users += 1;
        if (r.is_running) a.active_engines += 1;
        a.total_trades += r.total_trades;
        a.total_wins += r.wins;
        a.total_losses += r.losses;
        a.total_net_pnl_usd += r.net_pnl_usd;
        a.total_open_positions += r.open_positions;
        a.total_signals_24h += r.signals_24h;
        return a;
      },
      {
        users: 0, active_engines: 0, total_trades: 0, total_wins: 0, total_losses: 0,
        overall_win_rate: 0, total_net_pnl_usd: 0, total_open_positions: 0, total_signals_24h: 0,
      },
    );
    totals.overall_win_rate = totals.total_trades > 0
      ? (totals.total_wins / totals.total_trades) * 100
      : 0;

    rows.sort((a, b) => b.net_pnl_usd - a.net_pnl_usd);
    return { totals, users: rows };
  });

export const checkIsAdmin = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }) => {
    const { data, error } = await context.supabase.rpc("has_role", {
      _user_id: context.userId,
      _role: "admin",
    });
    if (error) return { isAdmin: false };
    return { isAdmin: !!data };
  });
