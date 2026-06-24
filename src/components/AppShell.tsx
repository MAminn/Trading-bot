import { Link, Outlet, useRouterState, useNavigate } from "@tanstack/react-router";
import { supabase } from "@/integrations/supabase/client";
import {
  LayoutDashboard,
  Link2,
  Sliders,
  History,
  BarChart3,
  Settings,
  LogOut,
  Search,
  Activity,
  Shield,
} from "lucide-react";
import { useIsAdmin } from "@/hooks/use-is-admin";
import { useEffect, useState, type ReactNode } from "react";
import { LogoMark } from "./Logo";
import { NotificationsBell } from "./NotificationsBell";
import { DemoModeBanner } from "./DemoModeBanner";
import { AlertsProvider } from "@/lib/alerts";
import { useEngineStatus, useEngineConfig, useEngineRealtime, liveState, fmtAgo } from "@/lib/engine";
import { useLivePrice } from "@/lib/live-price";

const NAV = [
  { to: "/app/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { to: "/app/engine", label: "ETHUSDT Engine", icon: Activity },
  { to: "/app/connect", label: "Connect Binance", icon: Link2 },
  { to: "/app/configure", label: "Configure", icon: Sliders },
  { to: "/app/history", label: "Trade History", icon: History },
  { to: "/app/reports", label: "Reports", icon: BarChart3 },
  { to: "/app/settings", label: "Settings", icon: Settings },
] as const;

const ADMIN_NAV = [
  { to: "/app/admin", label: "Admin", icon: Shield },
] as const;

function EngineStatusBadge() {
  const status = useEngineStatus();
  const config = useEngineConfig();
  const state = liveState(status.data, !!config.data?.is_running);
  const tone =
    state === "running" ? "bg-success" :
    state === "starting" ? "bg-warning" :
    state === "stale" ? "bg-warning" :
    state === "error" ? "bg-destructive" : "bg-muted-foreground";
  const label =
    state === "running" ? "Running" :
    state === "starting" ? "Starting…" :
    state === "stale" ? "No heartbeat" :
    state === "error" ? "Error" : "Stopped";
  return (
    <div className="flex items-center gap-2 rounded-lg border border-border bg-input/40 px-3 py-2 text-xs text-muted-foreground">
      <span className={`live-dot h-1.5 w-1.5 rounded-full ${tone}`} />
      Engine {label} · {fmtAgo(status.data?.last_heartbeat)}
    </div>
  );
}

function HeaderTicker() {
  const eth = useLivePrice("ETHUSDT");
  if (!eth) {
    return (
      <div className="hidden items-center gap-2 rounded-lg border border-border bg-card/50 px-3 py-1.5 font-mono text-xs text-muted-foreground md:flex">
        ETHUSDT · connecting…
      </div>
    );
  }
  const up = eth.changePct >= 0;
  return (
    <div className="hidden items-center gap-2 rounded-lg border border-border bg-card/50 px-3 py-1.5 font-mono text-xs md:flex">
      <span className="text-muted-foreground">ETH</span>
      <span className="text-foreground">
        ${eth.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
      </span>
      <span className={up ? "text-success" : "text-destructive"}>
        {up ? "+" : ""}{eth.changePct.toFixed(2)}%
      </span>
    </div>
  );
}

export function AppShell({ children }: { children?: ReactNode }) {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [plan, setPlan] = useState("pro · tier 2");
  const [avatarUrl, setAvatarUrl] = useState<string | null>(null);

  useEngineRealtime();
  const isAdmin = useIsAdmin();

  useEffect(() => {
    let active = true;
    async function load() {
      const { data } = await supabase.auth.getUser();
      const u = data.user;
      if (!u || !active) return;
      const meta = (u.user_metadata ?? {}) as Record<string, string>;
      setName(meta.full_name ?? "");
      setEmail(u.email ?? "");
      if (meta.plan) setPlan(meta.plan);
      if (meta.avatar_path) {
        const { data: signed } = await supabase.storage
          .from("avatars")
          .createSignedUrl(meta.avatar_path, 60 * 60);
        if (active) setAvatarUrl(signed?.signedUrl ?? null);
      } else {
        setAvatarUrl(null);
      }
    }
    load();
    const { data: sub } = supabase.auth.onAuthStateChange((event) => {
      if (event === "USER_UPDATED" || event === "SIGNED_IN" || event === "TOKEN_REFRESHED") {
        load();
      }
    });
    return () => {
      active = false;
      sub.subscription.unsubscribe();
    };
  }, []);

  const displayName = name || email || "Account";
  const initials = (name || email || "?")
    .split(/\s+/).map((p) => p[0]).filter(Boolean).slice(0, 2).join("").toUpperCase();

  async function handleSignOut() {
    await supabase.auth.signOut();
    navigate({ to: "/login", replace: true });
  }

  return (
    <AlertsProvider>
      <div className="relative min-h-screen bg-background text-foreground">
        <div className="pointer-events-none fixed inset-0 bg-aurora opacity-70" aria-hidden />
        <div className="pointer-events-none fixed inset-0 bg-grid opacity-60" aria-hidden />

        <div className="relative grid min-h-screen grid-cols-[260px_1fr]">
          <aside className="sticky top-0 flex h-screen flex-col border-r border-sidebar-border bg-sidebar/80 backdrop-blur-xl">
            <div className="px-5 py-5">
              <Link to="/"><LogoMark /></Link>
            </div>
            <div className="px-3"><EngineStatusBadge /></div>
            <nav className="mt-6 flex-1 space-y-1 px-3">
              {[...NAV, ...(isAdmin ? ADMIN_NAV : [])].map(({ to, label, icon: Icon }) => {
                const active = pathname === to || (to !== "/app/dashboard" && pathname.startsWith(to));
                return (
                  <Link key={to} to={to}
                    className={[
                      "group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-all",
                      active
                        ? "bg-sidebar-accent text-sidebar-accent-foreground shadow-[inset_0_0_0_1px_oklch(0.85_0.18_165/30%)]"
                        : "text-sidebar-foreground/80 hover:bg-sidebar-accent/50 hover:text-sidebar-accent-foreground",
                    ].join(" ")}>
                    <Icon className={["h-4 w-4 transition-colors", active ? "text-primary" : "text-muted-foreground group-hover:text-primary"].join(" ")} />
                    <span className="font-medium">{label}</span>
                    {active && <span className="ml-auto h-1.5 w-1.5 rounded-full bg-primary shadow-[0_0_8px_oklch(0.85_0.18_165)]" />}
                  </Link>
                );
              })}
            </nav>
            <div className="border-t border-sidebar-border p-3">
              <div className="flex items-center gap-3 rounded-lg p-2">
                <div className="grid h-9 w-9 place-items-center overflow-hidden rounded-full bg-gradient-to-br from-primary to-accent font-semibold text-primary-foreground">
                  {avatarUrl ? <img src={avatarUrl} alt="" className="h-full w-full object-cover" /> : initials || "?"}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium">{displayName}</div>
                  <div className="truncate text-xs text-muted-foreground">{plan}</div>
                </div>
                <button type="button" onClick={handleSignOut} className="rounded-md p-1.5 text-muted-foreground hover:bg-sidebar-accent hover:text-foreground" aria-label="Log out">
                  <LogOut className="h-4 w-4" />
                </button>
              </div>
            </div>
          </aside>

          <main className="flex min-w-0 flex-col">
            <DemoModeBanner />
            <header className="sticky top-0 z-20 border-b border-border bg-background/70 backdrop-blur-xl">
              <div className="flex items-center gap-4 px-8 py-3">
                <div className="relative flex-1 max-w-md">
                  <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                  <input
                    className="w-full rounded-lg border border-border bg-input/40 py-2 pl-9 pr-3 text-sm placeholder:text-muted-foreground/60 focus:border-primary/60 focus:outline-none focus:ring-2 focus:ring-primary/20"
                    placeholder="Search trades, symbols, signals…"
                  />
                </div>
                <HeaderTicker />
                <NotificationsBell />
              </div>
            </header>
            <div className="flex-1 px-8 py-8">{children ?? <Outlet />}</div>
          </main>
        </div>
      </div>
    </AlertsProvider>
  );
}
