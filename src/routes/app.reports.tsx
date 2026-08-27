import { createFileRoute } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { format, subDays } from "date-fns";
import { FileJson, FileSpreadsheet, Inbox, Printer, RefreshCw, Wallet } from "lucide-react";
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
import {
  useExecutedTrades, useAccountingRealtime, realPerformance, realTradesCsv,
  fmtCommission, netTone, type ExecutedTradeRow, type RealPerformance,
} from "@/lib/accounting";

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

  // Real Binance accounting, filtered by the same range control but computed
  // entirely from executed_trades. `capital` is not an input to any figure in
  // this block — a real result is never scaled by a config column.
  const executedQ = useExecutedTrades(2000);
  useAccountingRealtime();
  const realFiltered = useMemo(() => {
    const rows = executedQ.data ?? [];
    if (range === "all") return rows;
    const cutoff = subDays(new Date(), RANGE_DAYS[range]).getTime();
    return rows.filter((t) => new Date(t.exit_time).getTime() >= cutoff);
  }, [executedQ.data, range]);
  const real = useMemo(() => realPerformance(realFiltered), [realFiltered]);

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

  /** MODELLED strategy export. The P&L column names itself as such: it is
   *  net_pnl_rate x capital_usd, not a Binance figure. */
  function exportCSV() {
    const csv = [
      `# Helix Report · ${range} · STRATEGY / MODELLED`,
      `# modelled_pnl_usd = net_pnl_rate x ${capital} model baseline. NOT your Binance result.`,
      `# Generated: ${new Date().toISOString()}`,
      "",
      "trade_id,side,setup,entry_t,exit_t,entry,exit,prob,exit_reason,net_pnl_rate,modelled_pnl_usd",
      ...filtered.map((t: TradeRow) =>
        [t.trade_id ?? "", t.side ?? "", t.setup_name ?? "", t.entry_t ?? "", t.exit_t ?? "",
         t.entry ?? "", t.exit ?? "", t.prob ?? "", t.exit_reason ?? "", t.net_pnl_rate ?? "",
         tradePnlUsd(t, capital).toFixed(2)].join(",")),
    ].join("\n");
    downloadBlob(csv, `helix-report-strategy-modelled-${Date.now()}.csv`, "text/csv;charset=utf-8");
    toast.success("Strategy CSV exported");
  }

  /** REAL Binance accounting export. Separate file, separate columns, and every
   *  value comes from `executed_trades`. */
  function exportRealCSV() {
    const csv = [
      `# Helix Report · ${range} · REAL BINANCE ACCOUNTING`,
      "# Every figure below is from Binance fill and income records. Commission is",
      "# the amount Binance charged, never a fee-rate estimate.",
      `# Generated: ${new Date().toISOString()}`,
      "",
      realTradesCsv(realFiltered),
    ].join("\n");
    downloadBlob(csv, `helix-report-binance-accounting-${Date.now()}.csv`, "text/csv;charset=utf-8");
    toast.success("Binance accounting CSV exported");
  }

  function exportJSON() {
    downloadBlob(
      JSON.stringify(
        {
          generatedAt: new Date().toISOString(),
          range,
          strategyModelled: { capitalBaselineUsd: capital, metrics, trades: filtered },
          realBinance: { performance: real, trades: realFiltered },
        },
        null,
        2,
      ),
      `helix-report-${Date.now()}.json`,
      "application/json",
    );
    toast.success("JSON exported");
  }

  return (
    <div className="space-y-8">
      <BinanceTicker />

      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-[0.2em] text-primary">Performance</div>
          <h1 className="mt-2 font-display text-3xl font-semibold">Reports</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {range.toUpperCase()} · {real.trades} real Binance trades ·{" "}
            {metrics.totalTrades} strategy trades (modelled on a {fmtUSD(capital)} baseline)
          </p>
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
          <Button variant="outline" size="sm" onClick={() => { tradesQ.refetch(); executedQ.refetch(); }}><RefreshCw className="mr-1.5 h-3.5 w-3.5" /> Refresh</Button>
          <Button variant="outline" size="sm" onClick={exportRealCSV} disabled={!realFiltered.length}><FileSpreadsheet className="mr-1.5 h-3.5 w-3.5" /> Binance CSV</Button>
          <Button variant="outline" size="sm" onClick={exportCSV} disabled={!filtered.length}><FileSpreadsheet className="mr-1.5 h-3.5 w-3.5" /> Strategy CSV</Button>
          <Button variant="outline" size="sm" onClick={exportJSON} disabled={!filtered.length && !realFiltered.length}><FileJson className="mr-1.5 h-3.5 w-3.5" /> JSON</Button>
          <Button variant="outline" size="sm" onClick={() => window.print()}><Printer className="mr-1.5 h-3.5 w-3.5" /> Print</Button>
        </div>
      </div>

      <RealBinancePerformance
        perf={real}
        rows={realFiltered}
        loading={executedQ.isLoading}
        range={range}
      />

      {/* ================= STRATEGY / MODELLED, from here down =================
          Every figure below is computed by computeMetrics() from net_pnl_rate
          scaled by the capital_usd config column. None of it is the client's
          money, and each label says so. */}
      <div className="border-t border-border pt-8">
        <div className="flex flex-wrap items-baseline gap-2">
          <h2 className="font-display text-2xl font-semibold">Strategy performance</h2>
          <span className="text-xs uppercase tracking-widest text-muted-foreground">modelled</span>
        </div>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          Simulated on the {fmtUSD(capital)} model baseline — the strategy&apos;s percentage
          returns, not your Binance account.
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-5">
        <Kpi label="Net P&L (modelled)" value={fmtUSD(metrics.netPnl, true)} tone={metrics.netPnl >= 0 ? "good" : "bad"} />
        <Kpi label="Trades (modelled)" value={`${metrics.totalTrades}`} />
        <Kpi label="Win rate (modelled)" value={`${metrics.winRate.toFixed(1)}%`} tone={metrics.winRate >= 50 ? "good" : "bad"} />
        <Kpi label="Profit factor (modelled)" value={Number.isFinite(metrics.profitFactor) ? metrics.profitFactor.toFixed(2) : "∞"} tone={metrics.profitFactor >= 1 ? "good" : "bad"} />
        <Kpi label="Max drawdown (modelled)" value={fmtPct(metrics.maxDrawdown)} tone="warn" />
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
              <div className="mb-3 text-sm font-medium">
                Strategy equity curve
                <span className="ml-2 text-xs font-normal text-muted-foreground">
                  model baseline — not your wallet
                </span>
              </div>
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

/** The client's ACTUAL Binance performance, from `executed_trades`.
 *
 *  Kept above and apart from the strategy section, and computed without
 *  touching `capital_usd`: a real result is measured in the dollars Binance
 *  moved, never in a percentage return scaled by a configured baseline.
 *
 *  Wins and losses are counted on NET P&L, after commission — a trade that
 *  grossed +$0.50 and cost $0.84 in fees lost the client money, and calling it
 *  a win would report the strategy's result as the client's.
 */
function RealBinancePerformance({
  perf,
  rows,
  loading,
  range,
}: {
  perf: RealPerformance;
  rows: ExecutedTradeRow[];
  loading: boolean;
  range: RangeKey;
}) {
  return (
    <div className="space-y-4">
      <div>
        <div className="flex items-center gap-2">
          <Wallet className="h-4 w-4 text-primary" />
          <h2 className="font-display text-2xl font-semibold">Real Binance performance</h2>
        </div>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          {range.toUpperCase()} · what your Binance account actually did, after the exact
          commission Binance charged on every fill and the funding it paid or collected.
        </p>
      </div>

      {rows.length === 0 ? (
        <div className="card-elevated flex flex-col items-center justify-center gap-2 py-12 text-center">
          <Inbox className="h-6 w-6 text-muted-foreground" />
          <div className="text-sm font-medium">
            {loading ? "Loading Binance accounting…" : "No real Binance trades in this range"}
          </div>
          <p className="max-w-md text-xs text-muted-foreground">
            Figures appear once a position has opened and closed on your Binance account and
            the accounting sync has read its fills. Nothing here is estimated, so an absent
            result stays absent rather than showing as zero.
          </p>
        </div>
      ) : (
        <>
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
            <Kpi
              label="Real Net P&L"
              value={fmtUSD(perf.netPnl, true)}
              tone={netTone(perf.netPnl) === "success" ? "good" : "bad"}
              sub="after commission and funding"
              strong
            />
            <Kpi label="Gross P&L" value={fmtUSD(perf.grossPnl, true)} sub="before costs" />
            <Kpi
              label="Total commission"
              value={fmtCommission(perf.commission)}
              tone={perf.commission > 0 ? "bad" : undefined}
              sub="actual Binance fees"
            />
            <Kpi
              label="Funding"
              value={fmtUSD(perf.funding, true)}
              tone={perf.funding < 0 ? "bad" : perf.funding > 0 ? "good" : undefined}
              sub={perf.funding < 0 ? "paid" : perf.funding > 0 ? "received" : "none"}
            />
          </div>
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
            <Kpi label="Real completed trades" value={`${perf.trades}`} />
            <Kpi label="Real wins" value={`${perf.wins}`} tone={perf.wins ? "good" : undefined} />
            <Kpi label="Real losses" value={`${perf.losses}`} tone={perf.losses ? "bad" : undefined} />
            <Kpi
              label="Real win rate"
              value={`${perf.winRate.toFixed(1)}%`}
              tone={perf.winRate >= 50 ? "good" : "bad"}
              sub="measured on net, after fees"
            />
          </div>
          {perf.incompleteTrades > 0 && (
            <div className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-xs text-warning">
              {perf.incompleteTrades} executed trade{perf.incompleteTrades === 1 ? "" : "s"} in this
              range could not be priced exactly from Binance&apos;s records and{" "}
              {perf.incompleteTrades === 1 ? "is" : "are"} excluded from every figure above.
              They are listed individually on the Trade History page.
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Kpi({
  label, value, tone, sub, strong,
}: {
  label: string;
  value: string;
  tone?: "good" | "bad" | "warn";
  sub?: string;
  strong?: boolean;
}) {
  const color = tone === "good" ? "text-success" : tone === "bad" ? "text-destructive" : tone === "warn" ? "text-warning" : "text-foreground";
  return (
    <div className={`card-elevated p-5 ${strong ? "ring-1 ring-primary/25" : ""}`}>
      <div className="text-xs uppercase tracking-widest text-muted-foreground">{label}</div>
      <div className={`mt-2 font-mono font-semibold ${strong ? "text-3xl" : "text-xl"} ${color}`}>
        {value}
      </div>
      {sub && <div className="mt-1 text-xs text-muted-foreground">{sub}</div>}
    </div>
  );
}
