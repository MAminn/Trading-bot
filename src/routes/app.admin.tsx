import { createFileRoute, redirect } from "@tanstack/react-router";
import { useServerFn } from "@tanstack/react-start";
import { useQuery } from "@tanstack/react-query";
import { getAdminOverview, checkIsAdmin } from "@/lib/admin.functions";
import { fmtUSD, fmtPct, fmtAgo } from "@/lib/engine";
import { Users, Activity, TrendingUp, TrendingDown, Wallet, Radio, BarChart3 } from "lucide-react";

export const Route = createFileRoute("/app/admin")({
  component: AdminPage,
});

function StatCard({ icon: Icon, label, value, tone }: { icon: any; label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-xl border border-border bg-card/60 p-4 backdrop-blur">
      <div className="flex items-center gap-2 text-xs uppercase tracking-wide text-muted-foreground">
        <Icon className="h-3.5 w-3.5" /> {label}
      </div>
      <div className={`mt-2 font-mono text-2xl font-semibold ${tone ?? ""}`}>{value}</div>
    </div>
  );
}

function AdminPage() {
  const fetchOverview = useServerFn(getAdminOverview);
  const checkAdmin = useServerFn(checkIsAdmin);

  const adminCheck = useQuery({
    queryKey: ["admin", "check"],
    queryFn: () => checkAdmin({}),
  });

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "overview"],
    queryFn: () => fetchOverview({}),
    enabled: adminCheck.data?.isAdmin === true,
    refetchInterval: 15_000,
  });

  if (adminCheck.isLoading) {
    return <div className="text-sm text-muted-foreground">Checking access…</div>;
  }
  if (!adminCheck.data?.isAdmin) {
    return (
      <div className="rounded-xl border border-destructive/40 bg-destructive/10 p-6">
        <h2 className="text-lg font-semibold text-destructive">Access denied</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          You need the <code>admin</code> role to view this page.
        </p>
      </div>
    );
  }

  if (isLoading) return <div className="text-sm text-muted-foreground">Loading admin overview…</div>;
  if (error) {
    return (
      <div className="rounded-xl border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
        {(error as Error).message}
      </div>
    );
  }
  if (!data) return null;

  const t = data.totals;
  const pnlTone = t.total_net_pnl_usd >= 0 ? "text-success" : "text-destructive";

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Admin · User accounts</h1>
        <p className="text-sm text-muted-foreground">All users, their engine state, and aggregate performance.</p>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard icon={Users} label="Total users" value={t.users.toString()} />
        <StatCard icon={Activity} label="Active engines" value={t.active_engines.toString()} />
        <StatCard icon={BarChart3} label="Total trades" value={t.total_trades.toString()} />
        <StatCard icon={TrendingUp} label="Win rate" value={fmtPct(t.overall_win_rate)} />
        <StatCard icon={Wallet} label="Net P&L (all users)" value={fmtUSD(t.total_net_pnl_usd, true)} tone={pnlTone} />
        <StatCard icon={TrendingUp} label="Wins" value={t.total_wins.toString()} tone="text-success" />
        <StatCard icon={TrendingDown} label="Losses" value={t.total_losses.toString()} tone="text-destructive" />
        <StatCard icon={Radio} label="Signals (24h)" value={t.total_signals_24h.toString()} />
      </div>

      <div className="rounded-xl border border-border bg-card/60 backdrop-blur">
        <div className="border-b border-border px-4 py-3 text-sm font-medium">Users</div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-4 py-2 text-left">User</th>
                <th className="px-4 py-2 text-left">Engine</th>
                <th className="px-4 py-2 text-right">Capital</th>
                <th className="px-4 py-2 text-right">Trades</th>
                <th className="px-4 py-2 text-right">Win rate</th>
                <th className="px-4 py-2 text-right">Net P&L</th>
                <th className="px-4 py-2 text-right">Open</th>
                <th className="px-4 py-2 text-right">Signals 24h</th>
                <th className="px-4 py-2 text-left">Heartbeat</th>
              </tr>
            </thead>
            <tbody>
              {data.users.length === 0 && (
                <tr><td colSpan={9} className="px-4 py-8 text-center text-muted-foreground">No users yet.</td></tr>
              )}
              {data.users.map((u) => {
                const pnlT = u.net_pnl_usd > 0 ? "text-success" : u.net_pnl_usd < 0 ? "text-destructive" : "";
                const engineTone =
                  u.is_running && u.engine_status === "running" ? "bg-success" :
                  u.is_running ? "bg-warning" :
                  u.engine_status === "error" ? "bg-destructive" : "bg-muted-foreground";
                return (
                  <tr key={u.user_id} className="border-t border-border/60">
                    <td className="px-4 py-2">
                      <div className="font-medium">{u.full_name || u.email || u.user_id.slice(0, 8)}</div>
                      <div className="text-xs text-muted-foreground">{u.email}</div>
                    </td>
                    <td className="px-4 py-2">
                      <span className="inline-flex items-center gap-2 text-xs">
                        <span className={`h-1.5 w-1.5 rounded-full ${engineTone}`} />
                        {u.is_running ? "Running" : "Stopped"}
                        {u.demo_mode && <span className="rounded bg-warning/20 px-1.5 py-0.5 text-[10px] text-warning">DEMO</span>}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-right font-mono">{fmtUSD(u.capital_usd)}</td>
                    <td className="px-4 py-2 text-right font-mono">{u.total_trades}</td>
                    <td className="px-4 py-2 text-right font-mono">{fmtPct(u.win_rate)}</td>
                    <td className={`px-4 py-2 text-right font-mono ${pnlT}`}>{fmtUSD(u.net_pnl_usd, true)}</td>
                    <td className="px-4 py-2 text-right font-mono">{u.open_positions}</td>
                    <td className="px-4 py-2 text-right font-mono">{u.signals_24h}</td>
                    <td className="px-4 py-2 text-xs text-muted-foreground">{fmtAgo(u.last_heartbeat)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
