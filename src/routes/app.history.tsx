import { createFileRoute } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { Download, Inbox } from "lucide-react";
import { useTrades, useEngineConfig, useEngineStatus, fmtUSD, fmtPct, liveState, tradePnlUsd, type TradeRow } from "@/lib/engine";
import { SignalStatusBadge } from "@/components/SignalStatusBadge";
import { SignalTimelinePanel } from "@/components/SignalTimelinePanel";

export const Route = createFileRoute("/app/history")({
  head: () => ({ meta: [{ title: "Trade History — Helix" }] }),
  component: History,
});

type Filter = "All" | "Long" | "Short" | "Wins" | "Losses";

function durationStr(entry: string | null, exit: string | null) {
  if (!entry || !exit) return "—";
  const s = Math.max(0, Math.floor((new Date(exit).getTime() - new Date(entry).getTime()) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h ${m}m`;
}

function History() {
  const status = useEngineStatus();
  const cfg = useEngineConfig();
  const capital = Number(cfg.data?.capital_usd ?? 10000);
  const trades = useTrades(500);
  const [filter, setFilter] = useState<Filter>("All");
  const [timelineId, setTimelineId] = useState<string | null>(null);

  const rows = useMemo(() => {
    const list = trades.data ?? [];
    switch (filter) {
      case "Long": return list.filter((t) => (t.side ?? "").toUpperCase() === "LONG");
      case "Short": return list.filter((t) => (t.side ?? "").toUpperCase() === "SHORT");
      case "Wins": return list.filter((t) => Number(t.net_pnl_rate ?? 0) > 0);
      case "Losses": return list.filter((t) => Number(t.net_pnl_rate ?? 0) < 0);
      default: return list;
    }
  }, [trades.data, filter]);

  const totalPnl = (trades.data ?? []).reduce((a, t) => a + tradePnlUsd(t, capital), 0);
  const wins = (trades.data ?? []).filter((t) => Number(t.net_pnl_rate ?? 0) > 0).length;
  const state = liveState(status.data);

  function exportCsv() {
    const header = "id,trade_id,side,setup,entry_t,exit_t,entry,exit,tp,sl,prob,exit_reason,net_pnl_rate,pnl_usd";
    const csv = [header,
      ...(trades.data ?? []).map((t: TradeRow) =>
        [t.id, t.trade_id ?? "", t.side ?? "", t.setup_name ?? "", t.entry_t ?? "", t.exit_t ?? "",
         t.entry ?? "", t.exit ?? "", t.tp ?? "", t.sl ?? "", t.prob ?? "",
         t.exit_reason ?? "", t.net_pnl_rate ?? "", tradePnlUsd(t, capital).toFixed(2)].join(",")),
    ].join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `helix-trades-${Date.now()}.csv`; a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-[0.2em] text-primary">History</div>
          <h1 className="mt-2 font-display text-3xl font-semibold">Closed trades</h1>
          <p className="mt-1 text-sm text-muted-foreground">Every closed cycle from the engine — your full audit trail.</p>
        </div>
        <button onClick={exportCsv} disabled={!trades.data?.length}
          className="inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary to-accent px-4 py-2 text-sm font-semibold text-primary-foreground disabled:opacity-50">
          <Download className="h-4 w-4" /> Export CSV
        </button>
      </div>

      <div className="grid gap-4 md:grid-cols-4">
        <Tile label="Total trades" value={`${trades.data?.length ?? 0}`} />
        <Tile label="Wins" value={`${wins}`} tone="success" />
        <Tile label="Losses" value={`${(trades.data?.length ?? 0) - wins}`} tone="destructive" />
        <Tile label="Net P&L" value={fmtUSD(totalPnl, true)} tone={totalPnl >= 0 ? "success" : "destructive"} />
      </div>

      <div className="card-elevated overflow-hidden p-0">
        <div className="flex gap-2 border-b border-border p-3 text-xs">
          {(["All", "Long", "Short", "Wins", "Losses"] as Filter[]).map((c) => (
            <button key={c} onClick={() => setFilter(c)}
              className={`rounded-full px-3 py-1 ${filter === c ? "bg-primary/15 text-primary" : "text-muted-foreground hover:text-foreground"}`}>
              {c}
            </button>
          ))}
        </div>
        {rows.length > 0 ? (
          <table className="w-full text-sm">
            <thead className="bg-card/60 text-left text-xs uppercase tracking-wider text-muted-foreground">
              <tr>
                <th className="px-4 py-3">Setup</th>
                <th className="px-4 py-3">Entry</th>
                <th className="px-4 py-3">Side</th>
                <th className="px-4 py-3 text-right">Entry $</th>
                <th className="px-4 py-3 text-right">Exit $</th>
                <th className="px-4 py-3 text-right">P&amp;L</th>
                <th className="px-4 py-3 text-right">Return</th>
                <th className="px-4 py-3 text-right">Duration</th>
                <th className="px-4 py-3">Reason</th>
                <th className="px-4 py-3 text-right">Result</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border font-mono text-sm">
              {rows.map((t) => {
                const pnlUsd = tradePnlUsd(t, capital);
                const ret = Number(t.net_pnl_rate ?? 0) * 100;
                const sideUp = (t.side ?? "").toUpperCase();
                const win = Number(t.net_pnl_rate ?? 0) > 0;
                return (
                  <tr
                    key={t.id}
                    onClick={() => t.trade_id && setTimelineId(t.trade_id)}
                    className={`hover:bg-card/40 ${t.trade_id ? "cursor-pointer" : ""}`}
                  >
                    <td className="px-4 py-3 text-muted-foreground">{t.setup_name ?? "—"}</td>
                    <td className="px-4 py-3">{t.entry_t ? new Date(t.entry_t).toLocaleString() : "—"}</td>
                    <td className="px-4 py-3">
                      <span className={`rounded px-2 py-0.5 text-xs ${sideUp === "LONG" ? "bg-success/15 text-success" : "bg-destructive/15 text-destructive"}`}>{sideUp || "—"}</span>
                    </td>
                    <td className="px-4 py-3 text-right">{t.entry != null ? `$${Number(t.entry).toFixed(2)}` : "—"}</td>
                    <td className="px-4 py-3 text-right">{t.exit != null ? `$${Number(t.exit).toFixed(2)}` : "—"}</td>
                    <td className={`px-4 py-3 text-right ${pnlUsd >= 0 ? "text-success" : "text-destructive"}`}>{fmtUSD(pnlUsd, true)}</td>
                    <td className={`px-4 py-3 text-right ${ret >= 0 ? "text-success" : "text-destructive"}`}>{fmtPct(ret, true)}</td>
                    <td className="px-4 py-3 text-right text-muted-foreground">{durationStr(t.entry_t, t.exit_t)}</td>
                    <td className="px-4 py-3 text-xs text-muted-foreground">{t.exit_reason ?? "—"}</td>
                    <td className="px-4 py-3 text-right"><SignalStatusBadge status={win ? "CLOSED_WIN" : "CLOSED_LOSS"} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <div className="flex flex-col items-center justify-center gap-2 px-6 py-16 text-center">
            <Inbox className="h-6 w-6 text-muted-foreground" />
            <div className="text-sm font-medium">No closed trades yet</div>
            <p className="max-w-md text-xs text-muted-foreground">
              {state === "running" ? "Engine is running. Trades appear here as positions close." : "Start the engine on the Engine page. Closed trades will appear here."}
            </p>
          </div>
        )}
      </div>

      {timelineId && (
        <SignalTimelinePanel tradeId={timelineId} onClose={() => setTimelineId(null)} />
      )}
    </div>
  );
}

function Tile({ label, value, tone }: { label: string; value: string; tone?: "success" | "destructive" }) {
  const color = tone === "success" ? "text-success" : tone === "destructive" ? "text-destructive" : "text-foreground";
  return (
    <div className="card-elevated p-5">
      <div className="text-xs uppercase tracking-widest text-muted-foreground">{label}</div>
      <div className={`mt-2 font-mono text-2xl font-semibold ${color}`}>{value}</div>
    </div>
  );
}
