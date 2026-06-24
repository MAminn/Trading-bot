import { createFileRoute, Link } from "@tanstack/react-router";
import { ArrowRight, Shield, Zap, LineChart, Lock, Cpu, Activity, CircleDot } from "lucide-react";
import { LogoMark } from "@/components/Logo";
import { Sparkline } from "@/components/Sparkline";
import { useEngineStatus, useEngineConfig, useSignals, useTrades, computeMetrics, fmtUSD, fmtPct, liveState, signalSideLabel, fmtAgo } from "@/lib/engine";
import { useLivePrice } from "@/lib/live-price";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Helix — Live Crypto Trading Dashboard" },
      { name: "description", content: "Institutional-grade live trading for the ETHUSDT strategy. Connect Binance, configure risk, and let the engine run — signals first, full automation next." },
      { property: "og:title", content: "Helix — Live Crypto Trading Dashboard" },
      { property: "og:description", content: "Connect Binance, configure risk, and let the engine run." },
    ],
  }),
  component: Landing,
});

function Landing() {
  const status = useEngineStatus();
  const config = useEngineConfig();
  const signals = useSignals(10);
  const trades = useTrades(500);
  const eth = useLivePrice("ETHUSDT");
  const live = liveState(status.data) === "running";
  const capital = Number(config.data?.capital_usd ?? 10000);
  const metrics = computeMetrics(trades.data ?? [], capital);
  const equity = capital + metrics.netPnl;
  const lastSignal = signals.data?.[0];
  const lastSide = lastSignal ? signalSideLabel(lastSignal) : "FLAT";
  const sideClass = lastSide === "SHORT" ? "text-destructive" : lastSide === "LONG" ? "text-success" : "text-muted-foreground";

  return (
    <div className="relative min-h-screen overflow-hidden bg-background text-foreground">
      <div className="pointer-events-none absolute inset-0 bg-aurora" aria-hidden />
      <div className="pointer-events-none absolute inset-0 bg-grid opacity-50" aria-hidden />

      {/* Nav */}
      <header className="relative z-10 mx-auto flex max-w-7xl items-center justify-between px-6 py-6">
        <LogoMark />
        <nav className="hidden gap-8 text-sm text-muted-foreground md:flex">
          <a href="#flow" className="hover:text-foreground">How it works</a>
          <a href="#phases" className="hover:text-foreground">Phases</a>
          <a href="#safety" className="hover:text-foreground">Safety</a>
          <a href="#screens" className="hover:text-foreground">Product</a>
        </nav>
        <div className="flex items-center gap-3">
          <Link to="/login" className="hidden text-sm text-muted-foreground hover:text-foreground sm:inline">
            Log in
          </Link>
          <Link
            to="/app/dashboard"
            className="group inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary to-accent px-4 py-2 text-sm font-semibold text-primary-foreground shadow-[0_8px_24px_-8px_oklch(0.85_0.18_165/60%)] transition-transform hover:scale-[1.02]"
          >
            Launch dashboard <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
          </Link>
        </div>
      </header>

      {/* Hero */}
      <section className="relative z-10 mx-auto max-w-7xl px-6 pt-16 pb-24">
        <div className="grid items-center gap-12 lg:grid-cols-[1.1fr_1fr]">
          <div>
            <div className="inline-flex items-center gap-2 rounded-full border border-border bg-card/50 px-3 py-1 text-xs text-muted-foreground backdrop-blur">
              <span className={`live-dot h-1.5 w-1.5 rounded-full ${live ? "bg-success" : "bg-muted-foreground"}`} />
              Engine {(status.data?.status ?? "stopped").toUpperCase()} · {config.data?.mode === "auto" ? "Auto" : "Signals only"}
            </div>
            <h1 className="mt-6 font-display text-5xl font-semibold leading-[1.05] tracking-tight md:text-6xl lg:text-7xl">
              The trading desk
              <br />
              <span className="text-gradient">behind the dashboard.</span>
            </h1>
            <p className="mt-6 max-w-xl text-lg text-muted-foreground">
              Helix wraps a battle-tested Python strategy in a beautiful web cockpit.
              Connect Binance, configure risk, press Start — get live signals first, full
              auto-execution next. Your keys stay encrypted, your model stays on our side.
            </p>
            <div className="mt-8 flex flex-wrap gap-3">
              <Link
                to="/app/dashboard"
                className="group inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary to-accent px-5 py-3 font-semibold text-primary-foreground shadow-[0_10px_30px_-10px_oklch(0.85_0.18_165/70%)] transition-transform hover:scale-[1.02]"
              >
                Open the cockpit <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
              </Link>
              <Link
                to="/app/engine"
                className="inline-flex items-center gap-2 rounded-lg border border-border bg-card/40 px-5 py-3 font-medium text-foreground backdrop-blur hover:bg-card/70"
              >
                Manage engine
              </Link>
            </div>

            <div className="mt-10 grid grid-cols-3 gap-6 border-t border-border pt-6">
              <div>
                <div className="font-mono text-2xl font-semibold text-foreground">
                  {metrics.totalTrades > 0 ? `${metrics.winRate.toFixed(1)}%` : "—"}
                </div>
                <div className="text-xs uppercase tracking-wider text-muted-foreground">Win rate</div>
              </div>
              <div>
                <div className="font-mono text-2xl font-semibold text-foreground">
                  {metrics.totalTrades > 0 && Number.isFinite(metrics.profitFactor) ? metrics.profitFactor.toFixed(2) : metrics.totalTrades > 0 ? "∞" : "—"}
                </div>
                <div className="text-xs uppercase tracking-wider text-muted-foreground">Profit factor</div>
              </div>
              <div>
                <div className="font-mono text-2xl font-semibold text-foreground">
                  {metrics.totalTrades > 0 ? fmtPct(metrics.maxDrawdown) : "—"}
                </div>
                <div className="text-xs uppercase tracking-wider text-muted-foreground">Max drawdown</div>
              </div>
            </div>
          </div>

          {/* Hero glass card */}
          <div className="relative">
            <div className="absolute -inset-4 rounded-3xl bg-gradient-to-br from-primary/20 via-accent/10 to-transparent blur-2xl" aria-hidden />
            <div className="card-elevated relative overflow-hidden p-6">
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-xs uppercase tracking-widest text-muted-foreground">Equity</div>
                  <div className="mt-1 font-mono text-3xl font-semibold">{fmtUSD(equity)}</div>
                </div>
                <div className={`rounded-md border px-2 py-1 font-mono text-xs ${metrics.netPnl >= 0 ? "border-success/30 bg-success/10 text-success" : "border-destructive/30 bg-destructive/10 text-destructive"}`}>
                  {metrics.totalTrades > 0 ? fmtPct((metrics.netPnl / 10000) * 100, true) : "—"}
                </div>
              </div>
              <div className="mt-4">
                {metrics.equityCurve.length >= 2 ? (
                  <Sparkline data={metrics.equityCurve.map((p) => p.v)} height={120} />
                ) : (
                  <div className="grid h-[120px] place-items-center rounded-lg border border-dashed border-border/60 text-xs text-muted-foreground">
                    Equity curve appears after first closed trade
                  </div>
                )}
              </div>

              <div className="mt-6 grid grid-cols-2 gap-3">
                <div className="rounded-lg border border-border bg-card/60 p-3">
                  <div className="flex items-center justify-between text-xs text-muted-foreground">
                    <span>Current signal</span>
                    <CircleDot className={`h-3 w-3 ${lastSignal ? sideClass : "text-muted-foreground"}`} />
                  </div>
                  <div className={`mt-1 font-display text-xl font-semibold ${lastSignal ? sideClass : "text-muted-foreground"}`}>
                    {lastSignal ? (lastSide === "LONG" ? "BUY" : lastSide === "SHORT" ? "SELL" : "FLAT") : "—"}
                  </div>
                  <div className="font-mono text-[11px] text-muted-foreground">
                    {lastSignal?.ml_prob != null ? `conf · ${Number(lastSignal.ml_prob).toFixed(2)}` : fmtAgo(status.data?.last_heartbeat)}
                  </div>
                </div>
                <div className="rounded-lg border border-border bg-card/60 p-3">
                  <div className="text-xs text-muted-foreground">Net P&amp;L</div>
                  <div className={`mt-1 font-mono text-xl font-semibold ${metrics.netPnl >= 0 ? "text-success" : "text-destructive"}`}>
                    {metrics.totalTrades > 0 ? fmtUSD(metrics.netPnl, true) : "—"}
                  </div>
                  <div className="font-mono text-[11px] text-muted-foreground">{metrics.totalTrades} trades</div>
                </div>
              </div>

              <div className="mt-4 rounded-lg border border-border bg-card/60 p-3">
                <div className="flex items-center justify-between text-xs text-muted-foreground">
                  <span>ETH / USDT</span>
                  <span className="font-mono">
                    {eth ? `$${eth.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "—"}
                  </span>
                </div>
                <div className={`mt-1 text-[11px] font-mono ${(eth?.changePct ?? 0) >= 0 ? "text-success" : "text-destructive"}`}>
                  {eth ? fmtPct(eth.changePct, true) : "—"} · 24h · live from Binance
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Flow */}
      <section id="flow" className="relative z-10 mx-auto max-w-7xl px-6 py-20">
        <SectionHeader eyebrow="System flow" title="Three layers. One source of truth." />
        <div className="mt-10 grid gap-6 lg:grid-cols-3">
          <FlowLane
            tag="Lane A · Offline"
            color="from-accent/30 to-transparent"
            title="We train the model"
            items={[
              "Python training pipeline runs on our side",
              "Model + config + features published to artifact store",
              "Versioned, only latest approved goes live",
            ]}
            icon={<Cpu className="h-5 w-5" />}
          />
          <FlowLane
            tag="Lane B · Client"
            color="from-primary/30 to-transparent"
            title="You drive the cockpit"
            items={[
              "Log in, connect Binance (trade-only key)",
              "Set risk limits, capital allocation, mode",
              "Press Start — engine spins up for your account",
            ]}
            icon={<Activity className="h-5 w-5" />}
          />
          <FlowLane
            tag="Lane C · Engine"
            color="from-chart-3/30 to-transparent"
            title="The loop runs"
            items={[
              "Live market data · model · risk manager",
              "Phase 1: emits signals — Phase 2: auto-executes",
              "Every cycle logged, dashboard updated, until Stop",
            ]}
            icon={<Zap className="h-5 w-5" />}
          />
        </div>
      </section>

      {/* Phases */}
      <section id="phases" className="relative z-10 mx-auto max-w-7xl px-6 py-20">
        <SectionHeader eyebrow="Delivery" title="Two phases. Zero surprises." />
        <div className="mt-10 grid gap-6 lg:grid-cols-2">
          <PhaseCard
            label="Phase 1"
            title="Signals only"
            tone="primary"
            bullets={[
              "Full pipeline live: model → data → signal → dashboard",
              "Binance read-only is enough",
              "You trade manually if you want to",
              "Safe by design, fast to ship",
            ]}
          />
          <PhaseCard
            label="Phase 2"
            title="Auto-execution"
            tone="accent"
            bullets={[
              "Engine opens & closes orders on your portfolio",
              "Trade-only Binance permission (never withdrawals)",
              "Hard risk limits enforced before every order",
              "Visible kill-switch flattens positions instantly",
            ]}
          />
        </div>
      </section>

      {/* Safety */}
      <section id="safety" className="relative z-10 mx-auto max-w-7xl px-6 py-20">
        <SectionHeader eyebrow="Safety" title="Non-negotiables, in code." />
        <div className="mt-10 grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          {[
            { i: Lock, t: "Trade-only API keys", d: "Withdrawals are never requested or enabled." },
            { i: Shield, t: "Hard risk limits", d: "Max position, daily loss, leverage cap — enforced pre-trade." },
            { i: Zap, t: "Kill-switch", d: "One click halts the engine and (optionally) flattens positions." },
            { i: LineChart, t: "Full audit trail", d: "Every signal, every order, timestamped and queryable." },
          ].map(({ i: Icon, t, d }) => (
            <div key={t} className="card-elevated p-5">
              <div className="grid h-10 w-10 place-items-center rounded-lg bg-primary/10 text-primary">
                <Icon className="h-5 w-5" />
              </div>
              <div className="mt-4 font-display text-lg font-semibold">{t}</div>
              <p className="mt-1 text-sm text-muted-foreground">{d}</p>
            </div>
          ))}
        </div>
      </section>

      {/* CTA */}
      <section id="screens" className="relative z-10 mx-auto max-w-7xl px-6 py-24">
        <div className="card-elevated relative overflow-hidden p-10 text-center md:p-16">
          <div className="absolute inset-0 bg-aurora opacity-80" aria-hidden />
          <div className="relative">
            <h2 className="font-display text-3xl font-semibold tracking-tight md:text-5xl">
              Ready to <span className="text-gradient">go live</span>?
            </h2>
            <p className="mx-auto mt-3 max-w-xl text-muted-foreground">
              Open the cockpit, connect your exchange, and let the engine handle the loop.
            </p>
            <div className="mt-7 flex flex-wrap justify-center gap-3">
              <Link
                to="/app/dashboard"
                className="inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary to-accent px-6 py-3 font-semibold text-primary-foreground shadow-[0_10px_30px_-10px_oklch(0.85_0.18_165/70%)]"
              >
                Launch dashboard <ArrowRight className="h-4 w-4" />
              </Link>
              <Link to="/login" className="inline-flex items-center gap-2 rounded-lg border border-border bg-card/40 px-6 py-3 font-medium backdrop-blur hover:bg-card/70">
                Create account
              </Link>
            </div>
          </div>
        </div>
      </section>

      <footer className="relative z-10 border-t border-border">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-6 text-xs text-muted-foreground">
          <LogoMark />
          <span>© 2026 Helix Trading. Not financial advice.</span>
        </div>
      </footer>
    </div>
  );
}

function SectionHeader({ eyebrow, title }: { eyebrow: string; title: string }) {
  return (
    <div className="max-w-2xl">
      <div className="text-xs uppercase tracking-[0.2em] text-primary">{eyebrow}</div>
      <h2 className="mt-3 font-display text-3xl font-semibold tracking-tight md:text-4xl">{title}</h2>
    </div>
  );
}

function FlowLane({
  tag, title, items, icon, color,
}: { tag: string; title: string; items: string[]; icon: React.ReactNode; color: string }) {
  return (
    <div className="card-elevated relative overflow-hidden p-6">
      <div className={`absolute -right-10 -top-10 h-40 w-40 rounded-full bg-gradient-to-br ${color} blur-2xl`} aria-hidden />
      <div className="relative">
        <div className="flex items-center gap-2 text-xs uppercase tracking-widest text-muted-foreground">
          <span className="grid h-7 w-7 place-items-center rounded-md bg-card text-primary">{icon}</span>
          {tag}
        </div>
        <div className="mt-3 font-display text-xl font-semibold">{title}</div>
        <ul className="mt-4 space-y-2 text-sm text-muted-foreground">
          {items.map((it) => (
            <li key={it} className="flex gap-2">
              <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-primary" />
              <span>{it}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function PhaseCard({ label, title, bullets, tone }: { label: string; title: string; bullets: string[]; tone: "primary" | "accent" }) {
  const ring = tone === "primary" ? "ring-primary/30" : "ring-accent/30";
  const chip = tone === "primary" ? "bg-primary/15 text-primary" : "bg-accent/15 text-accent";
  return (
    <div className={`card-elevated relative p-7 ring-1 ${ring}`}>
      <div className={`inline-block rounded-full px-3 py-1 text-xs font-medium ${chip}`}>{label}</div>
      <h3 className="mt-3 font-display text-2xl font-semibold">{title}</h3>
      <ul className="mt-5 space-y-3 text-sm">
        {bullets.map((b) => (
          <li key={b} className="flex gap-3 text-muted-foreground">
            <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-primary" />
            <span className="text-foreground/90">{b}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
