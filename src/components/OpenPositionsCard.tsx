import { useState } from "react";
import { ChevronDown, ChevronRight, Square, Inbox, ExternalLink, X, Loader2 } from "lucide-react";
import { toast } from "sonner";
import {
  useOpenPositions, useEngineConfig, fmtUSD, fmtPct, useClosePosition,
  type OpenPositionRow,
} from "@/lib/engine";
import { SignalStatusBadge } from "@/components/SignalStatusBadge";
import { SignalTimelinePanel } from "@/components/SignalTimelinePanel";

export function OpenPositionsCard() {
  const { data: positions } = useOpenPositions();
  const { data: config } = useEngineConfig();
  const capital = Number(config?.capital_usd ?? 0);
  const leverage = Number(config?.leverage ?? 1);
  const [timelineId, setTimelineId] = useState<string | null>(null);
  const count = positions?.length ?? 0;

  return (
    <div className="card-elevated p-6">
      <div className="flex items-center justify-between gap-3">
        <div className="font-display text-lg font-semibold">Open positions</div>
        <span className="text-xs text-muted-foreground">{count} live</span>
      </div>
      <div className="mt-4 overflow-hidden rounded-lg border border-border">
        {positions && positions.length > 0 ? (
          <table className="w-full text-sm">
            <thead className="bg-card/60 text-left text-xs uppercase tracking-wider text-muted-foreground">
              <tr>
                <th className="px-4 py-2.5">Side</th>
                <th className="px-4 py-2.5">Entry</th>
                <th className="px-4 py-2.5">Stop</th>
                <th className="px-4 py-2.5 text-right">P&amp;L</th>
                <th className="px-4 py-2.5 text-center">Status</th>
                <th className="px-4 py-2.5 text-right">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border font-mono text-xs">
              {positions.map((p) => (
                <PositionRow
                  key={p.id}
                  pos={p}
                  capital={capital}
                  leverage={leverage}
                  onTimeline={setTimelineId}
                />
              ))}
            </tbody>
          </table>
        ) : (
          <div className="flex flex-col items-center justify-center gap-2 px-6 py-8 text-center">
            <div className="grid h-9 w-9 place-items-center rounded-full bg-card text-muted-foreground">
              <Inbox className="h-4 w-4" />
            </div>
            <div className="text-sm font-medium">No open positions</div>
            <div className="max-w-xs text-xs text-muted-foreground">
              When the engine opens a trade it will appear here in real time.
            </div>
          </div>
        )}
      </div>
      {timelineId && <SignalTimelinePanel tradeId={timelineId} onClose={() => setTimelineId(null)} />}
    </div>
  );
}

function PositionRow({ pos, capital, leverage, onTimeline }: {
  pos: OpenPositionRow; capital: number; leverage: number; onTimeline: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const close = useClosePosition();
  const isLong = (pos.side ?? "").toUpperCase() === "LONG";
  const sideColor = isLong ? "text-success" : "text-destructive";
  const sideBg = isLong ? "bg-success/15" : "bg-destructive/15";
  const pnlRate = Number(pos.unrealized_pnl_rate ?? 0);
  const pnlUsd = pnlRate * capital;
  const pnlUp = pnlRate >= 0;

  const entry = Number(pos.entry ?? 0);
  const stop = Number(pos.current_stop ?? pos.sl ?? 0);
  const distToStop = entry > 0 && stop > 0
    ? ((isLong ? entry - stop : stop - entry) / entry) * 100
    : null;
  const prob = pos.prob != null ? Number(pos.prob) : null;
  const thr = pos.threshold != null ? Number(pos.threshold) : null;

  async function handleClose(e: React.MouseEvent) {
    e.stopPropagation();
    if (close.isPending) return;
    if (!window.confirm(`Close ${pos.side} position at market?`)) return;
    try {
      await close.mutateAsync(pos.id);
      toast.success("Position closed");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to close position");
    }
  }

  return (
    <>
      <tr
        onClick={() => setOpen((v) => !v)}
        className="cursor-pointer hover:bg-card/40"
      >
        <td className="px-4 py-3">
          <div className="flex items-center gap-2">
            {open ? <ChevronDown className="h-3 w-3 text-muted-foreground" /> : <ChevronRight className="h-3 w-3 text-muted-foreground" />}
            <span className={`rounded px-1.5 py-0.5 font-mono text-xs font-semibold ${sideBg} ${sideColor}`}>
              {(pos.side ?? "—").toUpperCase()}
            </span>
          </div>
        </td>
        <td className="px-4 py-3 text-muted-foreground">{entry ? `$${entry.toFixed(2)}` : "—"}</td>
        <td className="px-4 py-3 text-muted-foreground">{stop ? `$${stop.toFixed(2)}` : "—"}</td>
        <td className={`px-4 py-3 text-right ${pnlUp ? "text-success" : "text-destructive"}`}>
          <div>{fmtUSD(pnlUsd, true)}</div>
          <div className="text-[10px] text-muted-foreground">{fmtPct(pnlRate * 100, true)}</div>
        </td>
        <td className="px-4 py-3 text-center">
          <SignalStatusBadge status="OPEN" />
        </td>
        <td className="px-4 py-3 text-right">
          <button
            type="button"
            onClick={handleClose}
            disabled={close.isPending}
            className="inline-flex items-center gap-1 rounded-md border border-destructive/40 bg-destructive/10 px-2.5 py-1.5 text-xs font-medium text-destructive hover:bg-destructive/20 disabled:opacity-60"
            title="Close position at market"
          >
            {close.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <X className="h-3 w-3" />}
            Close position
          </button>
        </td>
      </tr>

      {open && (
        <tr>
          <td colSpan={6} className="border-t border-border bg-card/30 px-4 py-4">
            <div className="mb-3 flex justify-end">
              <button
                type="button"
                onClick={() => onTimeline(pos.trade_id)}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card/60 px-2.5 py-1 text-xs hover:bg-card"
              >
                <ExternalLink className="h-3 w-3" /> View lifecycle
              </button>
            </div>
            <div className="grid gap-3 sm:grid-cols-3">
              <Detail k="Trade ID" v={pos.trade_id} />
              <Detail k="Setup" v={pos.setup_name ?? "—"} />
              <Detail k="Entry time" v={pos.entry_t ? new Date(pos.entry_t).toLocaleString() : "—"} />
              <Detail k="Entry price" v={entry ? `$${entry.toFixed(2)}` : "—"} />
              <Detail k="Stop loss" v={pos.sl != null ? `$${Number(pos.sl).toFixed(2)}` : "—"} />
              <Detail k="Take profit" v={pos.tp != null ? `$${Number(pos.tp).toFixed(2)}` : "—"} />
              <Detail k="Trailing stop" v={pos.current_stop != null ? `$${Number(pos.current_stop).toFixed(2)}` : "—"} />
              <Detail k="ATR" v={pos.atr != null ? Number(pos.atr).toFixed(2) : "—"} />
              <Detail k="Bars held" v={pos.bars_held?.toString() ?? "—"} />
              <Detail k="ML prob" v={prob != null ? prob.toFixed(3) : "—"} />
              <Detail k="Threshold" v={thr != null ? thr.toFixed(3) : "—"} />
              <Detail k="Unrealized P&L" v={`${fmtUSD(pnlUsd, true)} (${fmtPct(pnlRate * 100, true)})`} />
            </div>

            <div className="mt-4">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Leverage scenarios</div>
              <div className="mt-2 grid gap-2 sm:grid-cols-3">
                {[1, leverage, Math.max(leverage * 2, 10)].map((lev, i) => (
                  <div key={i} className="rounded-md border border-border bg-card/60 px-3 py-2 font-mono text-xs">
                    <div className="text-[10px] uppercase text-muted-foreground">at {lev}×</div>
                    <div className={pnlRate * lev >= 0 ? "text-success" : "text-destructive"}>
                      {fmtUSD(pnlRate * lev * capital, true)} ({fmtPct(pnlRate * lev * 100, true)})
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="mt-4 flex justify-end">
              <button
                type="button"
                disabled
                title="Phase 2 — auto-execute"
                className="inline-flex cursor-not-allowed items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs font-medium text-destructive opacity-60"
              >
                <Square className="h-3.5 w-3.5" /> Stop position
                <span className="ml-2 rounded bg-card px-1.5 py-0.5 text-[10px] text-muted-foreground">Phase 2</span>
              </button>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function Detail({ k, v }: { k: string; v: string }) {
  return (
    <div className="rounded-md border border-border bg-card/60 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{k}</div>
      <div className="truncate font-mono text-xs">{v}</div>
    </div>
  );
}
