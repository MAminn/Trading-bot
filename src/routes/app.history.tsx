import { createFileRoute } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { Download, Inbox, Receipt } from "lucide-react";
import { useTrades, useEngineConfig, useEngineStatus, fmtUSD, fmtPct, liveState, tradePnlUsd, type TradeRow } from "@/lib/engine";
import { SignalStatusBadge } from "@/components/SignalStatusBadge";
import { SignalTimelinePanel } from "@/components/SignalTimelinePanel";
import {
  useEngineOrders, ORDER_STATUS_LABEL, ORDER_REACHED_EXCHANGE, ORDER_FILLED,
  type EngineOrderRow,
} from "@/lib/executor";
import {
  useExecutedTrades, useAccountingRealtime, realPerformance, realTradesCsv,
  isComplete, fmtCommission, netTone, incompleteLabel, closeSourceLabel, UNAVAILABLE,
  type ExecutedTradeRow,
} from "@/lib/accounting";

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
  // LEGACY MODEL BASELINE. capital_usd is a config column (default 10,000),
  // not the Binance wallet, so every USD figure below is the strategy's
  // percentage return scaled by it — NOT realised Binance P&L.
  const capital = Number(cfg.data?.capital_usd ?? 10000);
  const trades = useTrades(500);
  const orders = useEngineOrders(200);
  // The real Binance accounting layer. Its own hook, its own table, its own
  // section below — never merged into the strategy rows.
  const executed = useExecutedTrades(500);
  useAccountingRealtime();
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

  function download(csv: string, name: string) {
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; a.click();
    URL.revokeObjectURL(url);
  }

  /** The MODELLED strategy export. `pnl_usd` here is net_pnl_rate x capital_usd,
   *  which the column name below says outright — it is not the client's money,
   *  and the real Binance export is a separate file. */
  function exportStrategyCsv() {
    const header = "id,trade_id,side,setup,entry_t,exit_t,entry,exit,tp,sl,prob,exit_reason,net_pnl_rate,modelled_pnl_usd";
    const csv = [header,
      ...(trades.data ?? []).map((t: TradeRow) =>
        [t.id, t.trade_id ?? "", t.side ?? "", t.setup_name ?? "", t.entry_t ?? "", t.exit_t ?? "",
         t.entry ?? "", t.exit ?? "", t.tp ?? "", t.sl ?? "", t.prob ?? "",
         t.exit_reason ?? "", t.net_pnl_rate ?? "", tradePnlUsd(t, capital).toFixed(2)].join(",")),
    ].join("\n");
    download(csv, `helix-strategy-trades-modelled-${Date.now()}.csv`);
  }

  return (
    <div className="space-y-10">
      <div>
        <div className="text-xs uppercase tracking-[0.2em] text-primary">History</div>
        <h1 className="mt-2 font-display text-3xl font-semibold">Trade history</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          Three separate records, in order of what they mean to you: what{" "}
          <strong>Binance</strong> actually charged and paid you, what the{" "}
          <strong>executor</strong> sent to Binance, and what the{" "}
          <strong>strategy</strong> modelled.
        </p>
      </div>

      {/* Layer 3, and the only one that is money. First on the page for that
          reason — the two below it describe intent and execution, this one
          describes the result. */}
      <RealBinanceTrades
        rows={executed.data ?? []}
        loading={executed.isLoading}
        onExport={() =>
          download(realTradesCsv(executed.data ?? []), `helix-binance-accounting-${Date.now()}.csv`)
        }
      />

      <div className="flex flex-wrap items-end justify-between gap-4 border-t border-border pt-8">
        <div>
          <h2 className="font-display text-2xl font-semibold">
            Strategy trades{" "}
            <span className="align-middle text-xs uppercase tracking-widest text-muted-foreground">
              modelled
            </span>
          </h2>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            What the <strong>strategy</strong> did, priced against the{" "}
            {fmtUSD(capital)} model baseline. These are simulated returns, not your
            Binance result.
          </p>
        </div>
        <button onClick={exportStrategyCsv} disabled={!trades.data?.length}
          className="inline-flex items-center gap-2 rounded-lg border border-border bg-card/40 px-4 py-2 text-sm font-semibold hover:bg-card/70 disabled:opacity-50">
          <Download className="h-4 w-4" /> Export strategy CSV (modelled)
        </button>
      </div>

      <div className="grid gap-4 md:grid-cols-4">
        <Tile label="Total trades" value={`${trades.data?.length ?? 0}`} />
        <Tile label="Wins" value={`${wins}`} tone="success" />
        <Tile label="Losses" value={`${(trades.data?.length ?? 0) - wins}`} tone="destructive" />
        <Tile label="Net P&L (modelled)" value={fmtUSD(totalPnl, true)} tone={totalPnl >= 0 ? "success" : "destructive"} />
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

      <ExchangeOrders rows={orders.data ?? []} loading={orders.isLoading} />

      {timelineId && (
        <SignalTimelinePanel tradeId={timelineId} onClose={() => setTimelineId(null)} />
      )}
    </div>
  );
}

/** The exchange layer of the history.
 *
 *  Kept as its own table rather than merged into the closed-trades list above,
 *  because these are three different facts and a client must be able to tell
 *  them apart:
 *
 *    1. the strategy opened/closed a position  -> the table above (user_trades)
 *    2. an order was SENT to Binance           -> here, status SENT
 *    3. the order FILLED, moving real money    -> here, status FILLED
 *
 *  A strategy trade with no matching filled order made no money and cost none.
 *  Presenting the two as one list is how a client comes to believe a paper
 *  result was a real one.
 */
function ExchangeOrders({ rows, loading }: { rows: EngineOrderRow[]; loading: boolean }) {
  const reached = rows.filter((r) => (ORDER_REACHED_EXCHANGE as readonly string[]).includes(r.status)).length;
  const filled = rows.filter((r) => (ORDER_FILLED as readonly string[]).includes(r.status)).length;

  return (
    <div className="space-y-4">
      <div>
        <h2 className="font-display text-2xl font-semibold">Binance orders</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          What the <strong>executor</strong> actually sent to your Binance account. An entry
          here with no <span className="font-mono">Filled</span> status never moved money.
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <Tile label="Order records" value={`${rows.length}`} />
        <Tile label="Sent to Binance" value={`${reached}`} />
        <Tile label="Filled" value={`${filled}`} tone={filled > 0 ? "success" : undefined} />
      </div>

      <div className="card-elevated overflow-hidden p-0">
        {rows.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-card/60 text-left text-xs uppercase tracking-wider text-muted-foreground">
                <tr>
                  <th className="px-4 py-3">When</th>
                  <th className="px-4 py-3">Intent</th>
                  <th className="px-4 py-3">Side</th>
                  <th className="px-4 py-3 text-right">Qty</th>
                  <th className="px-4 py-3 text-right">Notional</th>
                  <th className="px-4 py-3">Mode</th>
                  <th className="px-4 py-3">Outcome</th>
                  <th className="px-4 py-3">Binance ID</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const didFill = (ORDER_FILLED as readonly string[]).includes(r.status);
                  const failed = r.status === "FAILED";
                  return (
                    <tr key={r.id} className="border-t border-border/60">
                      <td className="px-4 py-3 text-xs text-muted-foreground">
                        {new Date(r.created_at).toLocaleString()}
                      </td>
                      <td className="px-4 py-3 font-mono text-xs">{r.intent}</td>
                      <td className="px-4 py-3 font-mono text-xs">{r.side}</td>
                      <td className="px-4 py-3 text-right font-mono text-xs">{r.qty ?? "—"}</td>
                      <td className="px-4 py-3 text-right font-mono text-xs">
                        {r.notional_usd === null ? "—" : fmtUSD(Number(r.notional_usd))}
                      </td>
                      <td className="px-4 py-3 font-mono text-xs">{r.execution_mode ?? "—"}</td>
                      <td className="px-4 py-3">
                        <span
                          className={
                            "rounded-full px-2 py-0.5 text-xs " +
                            (didFill
                              ? "bg-success/15 text-success"
                              : failed
                                ? "bg-destructive/15 text-destructive"
                                : "bg-muted text-muted-foreground")
                          }
                        >
                          {ORDER_STATUS_LABEL[r.status] ?? r.status}
                        </span>
                        {r.error && (
                          <div className="mt-1 max-w-xs truncate text-xs text-destructive" title={r.error}>
                            {r.error}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3 font-mono text-xs text-muted-foreground">
                        {r.binance_order_id ?? "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-3 p-10 text-center">
            <Inbox className="h-6 w-6 text-muted-foreground" />
            <div className="text-sm font-medium">
              {loading ? "Loading orders…" : "No Binance orders yet"}
            </div>
            <p className="max-w-md text-xs text-muted-foreground">
              Orders appear here once the executor is in a live trading mode with your
              Binance keys connected and auto-execute enabled.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

/** The real accounting layer: what Binance charged and paid.
 *
 *  The rows here are built from Binance's own fills and income records, not
 *  from a strategy return and not from a fee percentage. Deliberately its own
 *  table, above and apart from the strategy list, because a modelled +$120 and
 *  a realised +$11.66 are different kinds of fact.
 *
 *  Net P&L is the strongest column on the page: it is the only figure that
 *  answers "what did this trade do to my balance". Commission is always
 *  rendered with a leading minus, because a fee is money leaving the account
 *  even though it is stored as a positive cost.
 */
function RealBinanceTrades({
  rows,
  loading,
  onExport,
}: {
  rows: ExecutedTradeRow[];
  loading: boolean;
  onExport: () => void;
}) {
  const perf = realPerformance(rows);
  const exportable = rows.length > 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <Receipt className="h-4 w-4 text-primary" />
            <h2 className="font-display text-2xl font-semibold">Real Binance completed trades</h2>
          </div>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            Your <strong>actual</strong> result. Gross P&amp;L, commission and funding are read
            from Binance&apos;s own fill and income records — every commission is the amount
            Binance charged, never a fee-rate estimate.
          </p>
        </div>
        <button onClick={onExport} disabled={!exportable}
          className="inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary to-accent px-4 py-2 text-sm font-semibold text-primary-foreground disabled:opacity-50">
          <Download className="h-4 w-4" /> Export Binance accounting CSV
        </button>
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-5">
        <Tile label="Real Net P&L" value={perf.trades ? fmtUSD(perf.netPnl, true) : "—"}
          tone={perf.trades ? netTone(perf.netPnl) : undefined} strong />
        <Tile label="Gross P&L" value={perf.trades ? fmtUSD(perf.grossPnl, true) : "—"} />
        <Tile label="Commission" value={perf.trades ? fmtCommission(perf.commission) : "—"}
          tone={perf.trades && perf.commission > 0 ? "destructive" : undefined} />
        <Tile label="Funding" value={perf.trades ? fmtUSD(perf.funding, true) : "—"}
          tone={perf.trades && perf.funding !== 0 ? netTone(perf.funding) : undefined} />
        <Tile
          label="Completed trades"
          value={`${perf.trades}`}
          sub={
            perf.incompleteTrades
              ? `${perf.incompleteTrades} incomplete`
              : `${perf.wins}W / ${perf.losses}L`
          }
        />
      </div>

      <div className="card-elevated overflow-hidden p-0 ring-1 ring-primary/20">
        {rows.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-card/60 text-left text-xs uppercase tracking-wider text-muted-foreground">
                <tr>
                  <th className="px-4 py-3">Time</th>
                  <th className="px-4 py-3">Side</th>
                  <th className="px-4 py-3 text-right">Qty</th>
                  <th className="px-4 py-3 text-right">Entry</th>
                  <th className="px-4 py-3 text-right">Exit</th>
                  <th className="px-4 py-3 text-right">Gross P&amp;L</th>
                  <th className="px-4 py-3 text-right">Commission</th>
                  <th className="px-4 py-3 text-right">Funding</th>
                  <th className="px-4 py-3 text-right">Net P&amp;L</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border font-mono text-sm">
                {rows.map((t) => {
                  const complete = isComplete(t);
                  const sideUp = (t.side ?? "").toUpperCase();
                  const net = Number(t.net_pnl_usd);
                  // An incomplete row shows the trade and refuses the numbers.
                  // Rendering "$0.00" for something we could not price would be
                  // the estimate this whole layer exists to avoid.
                  const dash = <span className="text-muted-foreground">{UNAVAILABLE}</span>;
                  return (
                    <tr key={t.id} className="hover:bg-card/40">
                      <td className="px-4 py-3 text-xs text-muted-foreground">
                        {new Date(t.exit_time).toLocaleString()}
                        {(t.entry_fill_count > 1 || t.exit_fill_count > 1) && (
                          <div className="text-[10px]">
                            {t.entry_fill_count} entry / {t.exit_fill_count} exit fills
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <span className={`rounded px-2 py-0.5 text-xs ${sideUp === "LONG" ? "bg-success/15 text-success" : "bg-destructive/15 text-destructive"}`}>
                          {sideUp || "—"}
                        </span>
                        {/* Helix opened this position; if it did not close it,
                            the client needs to see that — the money is the same
                            either way, but the story of the trade is not. */}
                        {closeSourceLabel(t.close_source) && (
                          <div className="mt-1 text-[10px] text-warning">
                            {closeSourceLabel(t.close_source)}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right">{t.qty ?? "—"}</td>
                      <td className="px-4 py-3 text-right">
                        {t.entry_avg_price != null ? `$${Number(t.entry_avg_price).toFixed(2)}` : "—"}
                      </td>
                      <td className="px-4 py-3 text-right">
                        {t.exit_avg_price != null ? `$${Number(t.exit_avg_price).toFixed(2)}` : "—"}
                      </td>
                      <td className="px-4 py-3 text-right text-muted-foreground">
                        {complete ? fmtUSD(Number(t.gross_pnl_usd), true) : dash}
                      </td>
                      <td className="px-4 py-3 text-right text-destructive">
                        {complete ? fmtCommission(Number(t.commission_usd)) : dash}
                      </td>
                      <td className="px-4 py-3 text-right text-muted-foreground">
                        {complete ? fmtUSD(Number(t.funding_usd), true) : dash}
                      </td>
                      <td className="px-4 py-3 text-right">
                        {complete ? (
                          <span
                            className={`font-semibold ${net >= 0 ? "text-success" : "text-destructive"}`}
                          >
                            {fmtUSD(net, true)}
                          </span>
                        ) : (
                          <span className="text-xs text-warning" title={t.incomplete_reason ?? ""}>
                            {incompleteLabel(t.incomplete_reason)}
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-3 p-10 text-center">
            <Inbox className="h-6 w-6 text-muted-foreground" />
            <div className="text-sm font-medium">
              {loading ? "Loading Binance accounting…" : "No completed Binance trades yet"}
            </div>
            <p className="max-w-md text-xs text-muted-foreground">
              A trade appears here once a position has opened and closed on your Binance
              account and the accounting sync has read its fills. Until then nothing is
              shown — an empty result is never rendered as a zero.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

function Tile({
  label, value, tone, sub, strong,
}: {
  label: string;
  value: string;
  tone?: "success" | "destructive" | "muted";
  sub?: string;
  strong?: boolean;
}) {
  const color = tone === "success" ? "text-success" : tone === "destructive" ? "text-destructive" : tone === "muted" ? "text-muted-foreground" : "text-foreground";
  return (
    <div className={`card-elevated p-5 ${strong ? "ring-1 ring-primary/25" : ""}`}>
      <div className="text-xs uppercase tracking-widest text-muted-foreground">{label}</div>
      <div className={`mt-2 font-mono font-semibold ${strong ? "text-3xl" : "text-2xl"} ${color}`}>
        {value}
      </div>
      {sub && <div className="mt-1 text-xs text-muted-foreground">{sub}</div>}
    </div>
  );
}
