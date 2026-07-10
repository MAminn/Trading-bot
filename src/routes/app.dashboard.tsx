import { createFileRoute, Link } from "@tanstack/react-router";
import { useState } from "react";
import {
  Play,
  Square,
  ArrowUpRight,
  ArrowDownRight,
  CircleDot,
  Activity,
  AlertTriangle,
  Cpu,
  Inbox,
} from "lucide-react";
import { Sparkline } from "@/components/Sparkline";

import { SignalStatusBadge } from "@/components/SignalStatusBadge";
import { SignalTimelinePanel } from "@/components/SignalTimelinePanel";
import {
  useEngineStatus,
  useEngineConfig,
  useSignals,
  useTrades,
  useOpenPositions,
  useSetRunning,
  useCloseAllPositions,
  useClosePosition,
  computeMetrics,
  fmtUSD,
  fmtPct,
  fmtAgo,
  liveState,
  signalSideLabel,
  tradePnlUsd,
  signalStatus,
  STATUS_META,
  type SignalRow,
  type SignalStatus,
} from "@/lib/engine";
import { useLivePrice } from "@/lib/live-price";
import { toast } from "sonner";

export const Route = createFileRoute("/app/dashboard")({
  head: () => ({ meta: [{ title: "Dashboard — Helix" }] }),
  component: Dashboard,
});

function Dashboard() {
  const status = useEngineStatus();
  const config = useEngineConfig();
  const signals = useSignals(50);
  const trades = useTrades(500);
  const opens = useOpenPositions();
  const eth = useLivePrice("ETHUSDT");
  const setRunning = useSetRunning();
  const closeAll = useCloseAllPositions();
  const closeOne = useClosePosition();
  const [signalFilter, setSignalFilter] = useState<"all" | "accepted">("all");
  const [timelineId, setTimelineId] = useState<string | null>(null);

  const state = liveState(status.data, !!config.data?.is_running);
  const capital = Number(config.data?.capital_usd ?? 10000);
  const metrics = computeMetrics(trades.data ?? [], capital);
  const equity = capital + metrics.netPnl;

  const today = new Date().toDateString();
  const todayPnl = (trades.data ?? [])
    .filter((t) => t.exit_t && new Date(t.exit_t).toDateString() === today)
    .reduce((a, t) => a + tradePnlUsd(t, capital), 0);

  // Map trade_id → related open/closed records for status derivation.
  const openByTid = new Map((opens.data ?? []).map((p) => [p.trade_id, p]));
  const tradeByTid = new Map(
    (trades.data ?? []).filter((t) => t.trade_id).map((t) => [t.trade_id!, t]),
  );
  const enrichedSignals = (signals.data ?? []).map((s) => ({
    sig: s,
    status: signalStatus(s, {
      open: s.trade_id ? (openByTid.get(s.trade_id) ?? null) : null,
      trade: s.trade_id ? (tradeByTid.get(s.trade_id) ?? null) : null,
    }),
  }));
  const visibleSignals =
    signalFilter === "accepted"
      ? enrichedSignals.filter((e) => e.status !== "NO_SETUP" && e.status !== "REJECTED")
      : enrichedSignals;

  // Newest bar (usually a flat no_signal row) vs newest actionable signal
  // (rule_side !== 0). The Current Signal card features the actionable one.
  const latestBar = enrichedSignals[0];
  const latestActionable = enrichedSignals.find((e) => (e.sig.rule_side ?? 0) !== 0);
  const isRunning = !!config.data?.is_running;

  async function toggle() {
    try {
      await setRunning.mutateAsync(!isRunning);
      toast.success(!isRunning ? "Engine starting…" : "Engine stopping…");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to toggle engine");
    }
  }

  async function handleCloseAll() {
    const n = opens.data?.length ?? 0;
    if (n === 0) {
      toast.info("No open positions to close");
      return;
    }
    if (!window.confirm(`Close all ${n} open position${n === 1 ? "" : "s"} at market?`)) return;
    try {
      const r = await closeAll.mutateAsync();
      toast.success(`Closed ${r?.closed ?? n} position${(r?.closed ?? n) === 1 ? "" : "s"}`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to close positions");
    }
  }

  async function handleCloseOne(e: React.MouseEvent, openId: string) {
    e.stopPropagation();
    if (!window.confirm("Close this position at market?")) return;
    try {
      await closeOne.mutateAsync(openId);
      toast.success("Position closed");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to close position");
    }
  }

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-[0.2em] text-primary">Live cockpit</div>
          <h1 className="mt-2 font-display text-3xl font-semibold">
            ETHUSDT · {config.data?.mode === "auto" ? "Auto" : "Signals only"}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Engine <span className="font-mono text-foreground">{state.toUpperCase()}</span> ·
            heartbeat{" "}
            <span className="font-mono text-foreground">{fmtAgo(status.data?.last_heartbeat)}</span>{" "}
            · position{" "}
            <span className="font-mono text-foreground">
              {status.data?.current_position ?? "FLAT"}
            </span>
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={toggle}
            disabled={setRunning.isPending}
            className={`inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold shadow-[0_10px_30px_-10px_oklch(0.85_0.18_165/60%)] disabled:opacity-60 ${
              isRunning
                ? "bg-destructive text-destructive-foreground"
                : "bg-gradient-to-r from-primary to-accent text-primary-foreground"
            }`}
          >
            {isRunning ? <Square className="h-4 w-4" /> : <Play className="h-4 w-4" />}
            {isRunning ? "Stop engine" : "Start engine"}
          </button>
          <Link
            to="/app/engine"
            className="rounded-lg border border-border bg-card/40 px-3 py-2 text-sm hover:bg-card/70"
          >
            Engine →
          </Link>
        </div>
      </div>

      {state === "stale" && (
        <div className="flex items-start gap-3 rounded-lg border border-warning/30 bg-warning/10 p-4 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
          <div>
            <div className="font-medium text-warning">Engine is running but stale</div>
            <p className="mt-0.5 text-muted-foreground">
              No heartbeat in the last 3 minutes — check the engine.
            </p>
          </div>
        </div>
      )}
      {state === "error" && (
        <div className="flex items-start gap-3 rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
          <div>
            <div className="font-medium text-destructive">Engine reported an error</div>
            <p className="mt-0.5 text-muted-foreground">
              {status.data?.message ?? "See worker logs for details."}
            </p>
          </div>
        </div>
      )}
      {state === "stopped" && (
        <div className="flex items-start gap-3 rounded-lg border border-border bg-card/40 p-4 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <div>
            <div className="font-medium">Engine stopped</div>
            <p className="mt-0.5 text-muted-foreground">
              Press <strong>Start engine</strong> to flip the flag the Python worker polls. Signals
              appear here at the next bar close.
            </p>
          </div>
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <BigStat
          label="Equity"
          value={fmtUSD(equity)}
          delta={fmtPct((metrics.netPnl / capital) * 100, true)}
          up={metrics.netPnl >= 0}
          icon={<Activity className="h-4 w-4" />}
        />
        <BigStat
          label="Today P&L"
          value={fmtUSD(todayPnl, true)}
          delta="closed today"
          up={todayPnl >= 0}
          icon={
            todayPnl >= 0 ? (
              <ArrowUpRight className="h-4 w-4" />
            ) : (
              <ArrowDownRight className="h-4 w-4" />
            )
          }
        />
        <BigStat
          label="Win rate"
          value={metrics.totalTrades ? `${metrics.winRate.toFixed(1)}%` : "—"}
          delta={`${metrics.totalTrades} trades`}
          up={metrics.winRate >= 50}
          icon={<Activity className="h-4 w-4" />}
        />
        <BigStat
          label="Max drawdown"
          value={metrics.totalTrades ? fmtPct(metrics.maxDrawdown) : "—"}
          delta={`PF ${Number.isFinite(metrics.profitFactor) ? metrics.profitFactor.toFixed(2) : "∞"}`}
          icon={<ArrowDownRight className="h-4 w-4" />}
          muted
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="card-elevated p-6 lg:col-span-2">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-xs uppercase tracking-widest text-muted-foreground">
                Equity curve
              </div>
              <div className="mt-1 font-display text-2xl font-semibold">{fmtUSD(equity)}</div>
            </div>
            <span className="text-xs text-muted-foreground">
              starting {fmtUSD(capital)} · {metrics.equityCurve.length} closed trades
            </span>
          </div>
          <div className="mt-4">
            {metrics.equityCurve.length >= 2 ? (
              <Sparkline data={metrics.equityCurve.map((p) => p.v)} height={220} />
            ) : (
              <EmptyBox
                icon={<Activity className="h-5 w-5" />}
                title="No closed trades yet"
                sub="Equity curve appears after the first trade closes."
              />
            )}
          </div>
        </div>

        <CurrentSignalCard
          featured={latestActionable ?? latestBar}
          latestBar={latestBar}
          live={state === "running"}
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="card-elevated p-6 lg:col-span-2">
          <div className="flex items-center justify-between">
            <div className="font-display text-lg font-semibold">Live signal feed</div>
            <div className="flex items-center gap-2 text-xs">
              {(["all", "accepted"] as const).map((f) => (
                <button
                  key={f}
                  onClick={() => setSignalFilter(f)}
                  className={`rounded-full px-3 py-1 ${signalFilter === f ? "bg-primary/15 text-primary" : "text-muted-foreground hover:text-foreground"}`}
                >
                  {f === "all" ? "All" : "Accepted only"}
                </button>
              ))}
              <Link to="/app/history" className="text-primary hover:underline">
                View history →
              </Link>
              <button
                type="button"
                onClick={handleCloseAll}
                disabled={closeAll.isPending || (opens.data?.length ?? 0) === 0}
                className="rounded-full border border-destructive/40 bg-destructive/10 px-3 py-1 font-medium text-destructive hover:bg-destructive/20 disabled:opacity-50"
                title="Close every open position at market"
              >
                {closeAll.isPending
                  ? "Closing…"
                  : `Close all positions${opens.data?.length ? ` (${opens.data.length})` : ""}`}
              </button>
            </div>
          </div>
          <div className="mt-4 overflow-hidden rounded-lg border border-border">
            {visibleSignals.length > 0 ? (
              <table className="w-full text-sm">
                <thead className="bg-card/60 text-left text-xs uppercase tracking-wider text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2.5">Bar</th>
                    <th className="px-4 py-2.5">Side</th>
                    <th className="px-4 py-2.5">Reason</th>
                    <th className="px-4 py-2.5 text-right">ML p / thr</th>
                    <th className="px-4 py-2.5 text-right">Status</th>
                    <th className="px-4 py-2.5 text-right">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border font-mono text-xs">
                  {visibleSignals.slice(0, 12).map(({ sig: s, status }) => {
                    const side = signalSideLabel(s);
                    const openPos = s.trade_id ? openByTid.get(s.trade_id) : null;
                    return (
                      <tr
                        key={s.id}
                        onClick={() => s.trade_id && setTimelineId(s.trade_id)}
                        className={`hover:bg-card/40 ${s.trade_id ? "cursor-pointer" : ""}`}
                      >
                        <td className="px-4 py-3 text-muted-foreground">
                          {new Date(s.bar_time).toLocaleString()}
                        </td>
                        <td className="px-4 py-3">
                          <span
                            className={`rounded px-1.5 py-0.5 ${side === "LONG" ? "bg-success/15 font-bold text-success" : side === "SHORT" ? "bg-destructive/15 font-bold text-destructive" : "bg-muted/20 text-muted-foreground"}`}
                          >
                            {side}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-muted-foreground">{s.rule_reason ?? "—"}</td>
                        <td className="px-4 py-3 text-right text-muted-foreground">
                          {s.ml_prob != null ? Number(s.ml_prob).toFixed(2) : "—"}
                          {" / "}
                          {s.ml_threshold != null ? Number(s.ml_threshold).toFixed(2) : "—"}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <SignalStatusBadge status={status} />
                        </td>
                        <td className="px-4 py-3 text-right">
                          {openPos ? (
                            <button
                              type="button"
                              onClick={(e) => handleCloseOne(e, openPos.id)}
                              disabled={closeOne.isPending}
                              className="rounded-md border border-destructive/40 bg-destructive/10 px-2.5 py-1 text-[11px] font-medium text-destructive hover:bg-destructive/20 disabled:opacity-50"
                            >
                              Close position
                            </button>
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            ) : (
              <EmptyBox
                icon={<Inbox className="h-5 w-5" />}
                title="No signals yet"
                sub={
                  state === "running"
                    ? "Engine is running — a signal will appear at the next bar close."
                    : "Engine is idle. Signals appear here when it's running."
                }
              />
            )}
          </div>
        </div>

        <div className="card-elevated p-6">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-xs uppercase tracking-widest text-muted-foreground">
                ETH / USDT
              </div>
              <div className="mt-1 font-mono text-2xl font-semibold">
                {eth?.price
                  ? `$${eth.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
                  : "—"}
              </div>
              <div
                className={`text-xs ${(eth?.changePct ?? 0) >= 0 ? "text-success" : "text-destructive"}`}
              >
                {eth ? fmtPct(eth.changePct, true) : "—"} · 24h
              </div>
            </div>
            <Cpu className="h-5 w-5 text-muted-foreground" />
          </div>
          <div className="mt-6 grid grid-cols-2 gap-2 text-xs">
            <Mini label="24h high" value={eth ? `$${eth.high.toFixed(2)}` : "—"} />
            <Mini label="24h low" value={eth ? `$${eth.low.toFixed(2)}` : "—"} />
            <Mini label="Source" value={eth?.status ?? "connecting"} />
            <Mini label="Last bar" value={fmtAgo(status.data?.last_heartbeat)} />
          </div>
        </div>
      </div>

      <div className="card-elevated p-6">
        <div className="font-display text-lg font-semibold">Performance</div>
        {metrics.totalTrades > 0 ? (
          <div className="mt-4 grid gap-3 md:grid-cols-2 lg:grid-cols-4">
            <Stat label="Win rate" value={`${metrics.winRate.toFixed(1)}%`} bar={metrics.winRate} />
            <Stat
              label="Profit factor"
              value={Number.isFinite(metrics.profitFactor) ? metrics.profitFactor.toFixed(2) : "∞"}
              bar={Math.min(100, metrics.profitFactor * 30)}
            />
            <Stat
              label="Net P&L"
              value={fmtUSD(metrics.netPnl, true)}
              bar={Math.min(100, Math.abs(metrics.netPnl / capital) * 100)}
            />
            <Stat
              label="Max drawdown"
              value={fmtPct(metrics.maxDrawdown)}
              bar={Math.min(100, Math.abs(metrics.maxDrawdown) * 5)}
              tone="warn"
            />
          </div>
        ) : (
          <EmptyBox title="No trades yet" sub="Metrics populate after the first closed trade." />
        )}
      </div>

      {timelineId && (
        <SignalTimelinePanel tradeId={timelineId} onClose={() => setTimelineId(null)} />
      )}
    </div>
  );
}

function CurrentSignalCard({
  featured,
  latestBar,
  live,
}: {
  featured: { sig: SignalRow; status: SignalStatus } | undefined;
  latestBar: { sig: SignalRow; status: SignalStatus } | undefined;
  live: boolean;
}) {
  if (!featured) {
    return (
      <div className="card-elevated relative overflow-hidden p-6">
        <div className="text-xs uppercase tracking-widest text-muted-foreground">
          Current signal
        </div>
        <div className="mt-6">
          <EmptyBox
            title="Waiting for first signal"
            sub={
              live ? "Engine running — next bar close will produce a signal." : "Engine is idle."
            }
          />
        </div>
      </div>
    );
  }
  const { sig: signal } = featured;
  const side = signalSideLabel(signal);
  // NO SETUP: flat bar (rule_side === 0). VETO: sided setup rejected by ML.
  const isFlat = side === "FLAT";
  const vetoed = !isFlat && signal.ml_accept === false;
  const label = isFlat ? "NO SETUP" : vetoed ? "VETO" : side === "LONG" ? "BUY" : "SELL";
  const color = isFlat
    ? "text-muted-foreground"
    : vetoed
      ? "text-warning"
      : side === "LONG"
        ? "text-success"
        : "text-destructive";
  const showLatestBar = latestBar && latestBar.sig.id !== signal.id;

  return (
    <div className="card-elevated relative overflow-hidden p-6">
      <div
        className="absolute -right-12 -top-12 h-44 w-44 rounded-full bg-success/20 blur-3xl"
        aria-hidden
      />
      <div className="relative">
        <div className="flex items-center justify-between">
          <div className="text-xs uppercase tracking-widest text-muted-foreground">
            Current signal
          </div>
          {live && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-success/30 bg-success/10 px-2 py-0.5 text-[10px] font-medium text-success">
              <span className="live-dot h-1 w-1 rounded-full bg-success" /> LIVE
            </span>
          )}
        </div>
        <div className="mt-4 flex items-baseline gap-3">
          <CircleDot className={`h-6 w-6 ${color}`} />
          <div
            className={`font-display font-semibold ${isFlat ? "text-4xl" : "text-5xl"} ${color}`}
          >
            {label}
          </div>
        </div>
        <div className="mt-1 font-mono text-xs text-muted-foreground">
          confidence {signal.ml_prob != null ? Number(signal.ml_prob).toFixed(2) : "—"} ·{" "}
          {fmtAgo(signal.bar_time)}
        </div>
        {showLatestBar && (
          <div className="mt-2 font-mono text-xs text-muted-foreground">
            Latest bar: {STATUS_META[latestBar.status].label} · {fmtAgo(latestBar.sig.bar_time)}
          </div>
        )}
        <div className="mt-6 space-y-3">
          <Row label="Reason" value={signal.rule_reason ?? "—"} />
          <Row
            label="Threshold"
            value={signal.ml_threshold != null ? Number(signal.ml_threshold).toFixed(2) : "—"}
          />
          <Row
            label="Position"
            value={`${signal.position_before ?? "?"} → ${signal.position_after ?? "?"}`}
          />
          {signal.opened && <Row label="Opened" value={signal.opened} valueClass="text-success" />}
          {signal.closed_reason && (
            <Row label="Closed" value={signal.closed_reason} valueClass="text-muted-foreground" />
          )}
        </div>
        <Link
          to="/app/history"
          className="mt-6 block w-full rounded-lg border border-border bg-card/60 py-2 text-center text-sm font-medium hover:bg-card"
        >
          View signal log →
        </Link>
      </div>
    </div>
  );
}

function BigStat({
  label,
  value,
  delta,
  up,
  icon,
  muted,
}: {
  label: string;
  value: string;
  delta: string;
  up?: boolean;
  icon: React.ReactNode;
  muted?: boolean;
}) {
  return (
    <div className="card-elevated p-5">
      <div className="flex items-center justify-between text-xs uppercase tracking-widest text-muted-foreground">
        <span>{label}</span>
        <span
          className={muted ? "text-muted-foreground" : up ? "text-success" : "text-destructive"}
        >
          {icon}
        </span>
      </div>
      <div className="mt-2 font-mono text-2xl font-semibold">{value}</div>
      <div
        className={`mt-1 text-xs ${muted ? "text-muted-foreground" : up ? "text-success" : "text-destructive"}`}
      >
        {delta}
      </div>
    </div>
  );
}

function Row({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="flex items-center justify-between border-b border-border/60 pb-2 text-sm last:border-0 last:pb-0">
      <span className="text-muted-foreground">{label}</span>
      <span className={`font-mono ${valueClass ?? ""}`}>{value}</span>
    </div>
  );
}

function Mini({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-card/60 px-2.5 py-2">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="font-mono text-sm">{value}</div>
    </div>
  );
}

function Stat({
  label,
  value,
  bar,
  tone,
}: {
  label: string;
  value: string;
  bar: number;
  tone?: "warn";
}) {
  const color = tone === "warn" ? "bg-warning" : "bg-gradient-to-r from-primary to-accent";
  return (
    <div>
      <div className="flex items-center justify-between text-sm">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-mono font-semibold">{value}</span>
      </div>
      <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-card">
        <div
          className={`h-full ${color}`}
          style={{ width: `${Math.min(100, Math.max(4, bar))}%` }}
        />
      </div>
    </div>
  );
}

function EmptyBox({ title, sub, icon }: { title: string; sub?: string; icon?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-10 text-center">
      <div className="grid h-9 w-9 place-items-center rounded-full bg-card text-muted-foreground">
        {icon ?? <Inbox className="h-4 w-4" />}
      </div>
      <div className="text-sm font-medium">{title}</div>
      {sub && <div className="max-w-xs text-xs text-muted-foreground">{sub}</div>}
    </div>
  );
}
