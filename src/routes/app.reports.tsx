import { createFileRoute } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { format, subDays } from "date-fns";
import { FileJson, FileSpreadsheet, Inbox, Printer, RefreshCw } from "lucide-react";
import {
  Area, AreaChart, Bar as RBar, BarChart, CartesianGrid, Cell, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { Button } from "@/components/ui/button";
import { BinanceTicker } from "@/components/BinanceTicker";
import { toast } from "sonner";
import {
  computeMetrics, useEngineStatus, useEngineConfig, useTrades,
  fmtUSD, fmtPct, liveState, tradePnlUsd, type TradeRow,
} from "@/lib/engine";

export const Route = createFileRoute("/app/reports")({
  head: () => ({ meta: [{ title: "Reports — Helix" }] }),
  component: Reports,
});

type RangeKey = "7d" | "30d" | "90d" | "1y" | "all";
const RANGE_DAYS: Record<Exclude<RangeKey, "all">, number> = { "7d": 7, "30d": 30, "90d": 90, "1y": 365 };

function Reports() {
  const status = useEngineStatus();
  const cfg = useEngineConfig();
  const capital = Number(cfg.data?.capital_usd ?? 10000);
  const tradesQ = useTrades(2000);
  const allTrades = tradesQ.data ?? [];
  const [range, setRange] = useState<RangeKey>("30d");

  const filtered = useMemo(() => {
    if (range === "all") return allTrades;
    const cutoff = subDays(new Date(), RANGE_DAYS[range]).getTime();
    return allTrades.filter((t) => {
      const ts = t.exit_t ?? t.entry_t;
      return ts ? new Date(ts).getTime() >= cutoff : false;
    });
  }, [allTrades, range]);

  const metrics = useMemo(() => computeMetrics(filtered, capital), [filtered, capital]);

  const equitySeries = useMemo(
    () => metrics.equityCurve.map((p) => ({ date: format(new Date(p.iso), "MMM d"), iso: p.iso, equity: p.v })),
    [metrics.equityCurve],
  );

  const drawdown = useMemo(() => {
    let peak = -Infinity;
    return metrics.equityCurve.map((p) => {
      peak = Math.max(peak, p.v);
      return { date: format(new Date(p.iso), "MMM d"), dd: +(((p.v - peak) / peak) * 100).toFixed(2) };
    });
  }, [metrics.equityCurve]);

  const monthly = useMemo(() => {
    const buckets = new Map<string, { first: number; last: number }>();
    for (const p of metrics.equityCurve) {
      const key = p.iso.slice(0, 7);
      const b = buckets.get(key);
      if (!b) buckets.set(key, { first: p.v, last: p.v });
      else b.last = p.v;
    }
    return Array.from(buckets.entries()).slice(-12).map(([k, b]) => ({
      month: format(new Date(`${k}-01`), "MMM yy"),
      ret: +(((b.last - b.first) / b.first) * 100).toFixed(2),
    }));
  }, [metrics.equityCurve]);

  const live = liveState(status.data) === "running";

  function downloadBlob(content: string, name: string, type: string) {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; a.click();
    URL.revokeObjectURL(url);
  }

  function exportCSV() {
    const csv = [
      `# Helix Report · ${range}`,
      `# Generated: ${new Date().toISOString()}`,
      "",
      "trade_id,side,setup,entry_t,exit_t,entry,exit,prob,exit_reason,net_pnl_rate,pnl_usd",
      ...filtered.map((t: TradeRow) =>
        [t.trade_id ?? "", t.side ?? "", t.setup_name ?? "", t.entry_t ?? "", t.exit_t ?? "",
         t.entry ?? "", t.exit ?? "", t.prob ?? "", t.exit_reason ?? "", t.net_pnl_rate ?? "",
         tradePnlUsd(t, capital).toFixed(2)].join(",")),
    ].join("\n");
    downloadBlob(csv, `helix-report-${Date.now()}.csv`, "text/csv;charset=utf-8");
    toast.success("CSV exported");
  }

  function exportJSON() {
    downloadBlob(JSON.stringify({ generatedAt: new Date().toISOString(), range, metrics, trades: filtered }, null, 2),
      `helix-report-${Date.now()}.json`, "application/json");
    toast.success("JSON exported");
  }

  return (
    <div className="space-y-8">
      <BinanceTicker />

      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-[0.2em] text-primary">Performance</div>
          <h1 className="mt-2 font-display text-3xl font-semibold">Reports</h1>
          <p className="mt-1 text-sm text-muted-foreground">{range.toUpperCase()} · {metrics.totalTrades} closed trades · {fmtUSD(capital)} base</p>
        </div>

        <div className="flex flex-wrap items-center gap-2 print:hidden">
          <div className="inline-flex rounded-md border border-border bg-card p-0.5" role="group">
            {(["7d", "30d", "90d", "1y", "all"] as RangeKey[]).map((r) => (
              <button key={r} onClick={() => setRange(r)} aria-pressed={range === r}
                className={`rounded px-2.5 py-1 text-xs font-medium ${range === r ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"}`}>
                {r.toUpperCase()}
              </button>
            ))}
          </div>
          <Button variant="outline" size="sm" onClick={() => tradesQ.refetch()}><RefreshCw className="mr-1.5 h-3.5 w-3.5" /> Refresh</Button>
          <Button variant="outline" size="sm" onClick={exportCSV} disabled={!filtered.length}><FileSpreadsheet className="mr-1.5 h-3.5 w-3.5" /> CSV</Button>
          <Button variant="outline" size="sm" onClick={exportJSON} disabled={!filtered.length}><FileJson className="mr-1.5 h-3.5 w-3.5" /> JSON</Button>
          <Button variant="outline" size="sm" onClick={() => window.print()}><Printer className="mr-1.5 h-3.5 w-3.5" /> Print</Button>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-5">
        <Kpi label="Net P&L" value={fmtUSD(metrics.netPnl, true)} tone={metrics.netPnl >= 0 ? "good" : "bad"} />
        <Kpi label="Trades" value={`${metrics.totalTrades}`} />
        <Kpi label="Win rate" value={`${metrics.winRate.toFixed(1)}%`} tone={metrics.winRate >= 50 ? "good" : "bad"} />
        <Kpi label="Profit factor" value={Number.isFinite(metrics.profitFactor) ? metrics.profitFactor.toFixed(2) : "∞"} tone={metrics.profitFactor >= 1 ? "good" : "bad"} />
        <Kpi label="Max drawdown" value={fmtPct(metrics.maxDrawdown)} tone="warn" />
      </div>

      {metrics.totalTrades === 0 ? (
        <div className="card-elevated flex flex-col items-center justify-center gap-3 py-20 text-center">
          <Inbox className="h-7 w-7 text-muted-foreground" />
          <div className="text-base font-medium">No data in this range</div>
          <p className="max-w-md text-sm text-muted-foreground">
            {live ? "Engine running, no trades closed in this window yet." : "Start the engine on the Engine page."}
          </p>
        </div>
      ) : (
        <>
          <div className="grid gap-6 lg:grid-cols-3">
            <div className="card-elevated p-6 lg:col-span-2">
              <div className="mb-3 text-sm font-medium">Equity curve</div>
              <ResponsiveContainer width="100%" height={280}>
                <AreaChart data={equitySeries}>
                  <defs>
                    <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="oklch(0.85 0.18 165)" stopOpacity={0.5} />
                      <stop offset="100%" stopColor="oklch(0.85 0.18 165)" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.3 0 0 / 30%)" />
                  <XAxis dataKey="date" stroke="oklch(0.6 0 0)" fontSize={11} />
                  <YAxis stroke="oklch(0.6 0 0)" fontSize={11} />
                  <Tooltip contentStyle={{ background: "oklch(0.15 0 0)", border: "1px solid oklch(0.3 0 0)" }} />
                  <Area type="monotone" dataKey="equity" stroke="oklch(0.85 0.18 165)" fill="url(#eq)" strokeWidth={2} />
                </AreaChart>
              </ResponsiveContainer>
            </div>

            <div className="card-elevated p-6">
              <div className="mb-3 text-sm font-medium">Drawdown</div>
              <ResponsiveContainer width="100%" height={280}>
                <LineChart data={drawdown}>
                  <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.3 0 0 / 30%)" />
                  <XAxis dataKey="date" stroke="oklch(0.6 0 0)" fontSize={11} />
                  <YAxis stroke="oklch(0.6 0 0)" fontSize={11} />
                  <Tooltip contentStyle={{ background: "oklch(0.15 0 0)", border: "1px solid oklch(0.3 0 0)" }} />
                  <Line type="monotone" dataKey="dd" stroke="oklch(0.7 0.18 30)" strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>

          <div className="card-elevated p-6">
            <div className="mb-3 text-sm font-medium">Monthly returns</div>
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={monthly}>
                <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.3 0 0 / 30%)" />
                <XAxis dataKey="month" stroke="oklch(0.6 0 0)" fontSize={11} />
                <YAxis stroke="oklch(0.6 0 0)" fontSize={11} />
                <Tooltip contentStyle={{ background: "oklch(0.15 0 0)", border: "1px solid oklch(0.3 0 0)" }} />
                <RBar dataKey="ret">
                  {monthly.map((m, i) => (
                    <Cell key={i} fill={m.ret >= 0 ? "oklch(0.75 0.17 165)" : "oklch(0.65 0.2 25)"} />
                  ))}
                </RBar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
    </div>
  );
}

function Kpi({ label, value, tone }: { label: string; value: string; tone?: "good" | "bad" | "warn" }) {
  const color = tone === "good" ? "text-success" : tone === "bad" ? "text-destructive" : tone === "warn" ? "text-warning" : "text-foreground";
  return (
    <div className="card-elevated p-5">
      <div className="text-xs uppercase tracking-widest text-muted-foreground">{label}</div>
      <div className={`mt-2 font-mono text-xl font-semibold ${color}`}>{value}</div>
    </div>
  );
}
