// Signal lifecycle timeline panel — renders the six stages of a single trade
// (Evaluated → Setup → ML accepted/rejected → Opened → Managed → Closed) by
// joining user_signals + open_positions + user_trades on trade_id.
import { X, Check, Clock, ArrowUpRight, ArrowDownRight } from "lucide-react";
import {
  useSignalTimeline, signalSideLabel, signalStatus, fmtPct, fmtAgo,
  type SignalRow, type OpenPositionRow, type TradeRow,
} from "@/lib/engine";
import { SignalStatusBadge } from "@/components/SignalStatusBadge";

interface Stage {
  key: string;
  label: string;
  detail?: string;
  ts?: string | null;
  reached: boolean;
  tone?: "ok" | "veto" | "win" | "loss";
}

function buildStages(
  signals: SignalRow[],
  open: OpenPositionRow | null,
  trade: TradeRow | null,
): Stage[] {
  // First evaluation row for this trade_id (entry-side); fall back to first signal.
  const firstEval = signals[0] ?? null;
  const acceptedSig = signals.find((s) => s.opened) ?? signals.find((s) => s.ml_accept === true) ?? null;
  const rejectedSig = signals.find((s) => s.ml_accept === false) ?? null;
  const closeSig = signals.find((s) => s.closed_reason) ?? null;

  const side = firstEval ? signalSideLabel(firstEval) : "FLAT";
  const setupReached = !!firstEval && side !== "FLAT";
  const mlReached = !!firstEval && firstEval.ml_prob != null;
  const mlAccepted = !!acceptedSig || !!open || !!trade;
  const mlRejected = !mlAccepted && !!rejectedSig;
  const openedReached = !!open || !!trade || !!acceptedSig?.opened;
  const closedReached = !!trade;
  const win = trade ? Number(trade.net_pnl_rate ?? 0) > 0 : false;

  return [
    {
      key: "evaluated",
      label: "Evaluated",
      detail: firstEval ? `bar ${new Date(firstEval.bar_time).toLocaleString()}` : undefined,
      ts: firstEval?.bar_time,
      reached: !!firstEval,
      tone: "ok",
    },
    {
      key: "setup",
      label: setupReached ? `Setup · ${side}` : "Setup",
      detail: firstEval?.rule_reason ?? undefined,
      ts: firstEval?.bar_time,
      reached: setupReached,
      tone: "ok",
    },
    {
      key: "ml",
      label: mlRejected ? "ML rejected" : "ML accepted",
      detail: mlReached && firstEval
        ? `p ${Number(firstEval.ml_prob).toFixed(2)} / thr ${Number(firstEval.ml_threshold ?? 0).toFixed(2)}`
        : undefined,
      ts: (rejectedSig ?? acceptedSig ?? firstEval)?.bar_time,
      reached: mlReached && (mlAccepted || mlRejected),
      tone: mlRejected ? "veto" : "ok",
    },
    {
      key: "opened",
      label: "Opened",
      detail: open
        ? `entry $${Number(open.entry ?? 0).toFixed(2)}`
        : trade
        ? `entry $${Number(trade.entry ?? 0).toFixed(2)}`
        : undefined,
      ts: open?.entry_t ?? trade?.entry_t ?? acceptedSig?.bar_time,
      reached: openedReached,
      tone: "ok",
    },
    {
      key: "managed",
      label: "Managed",
      detail: open
        ? `${open.bars_held ?? 0} bars · stop $${Number(open.current_stop ?? 0).toFixed(2)}`
        : trade
        ? `${trade.bars_held ?? 0} bars held`
        : undefined,
      ts: open?.updated_at ?? trade?.entry_t,
      reached: openedReached,
      tone: "ok",
    },
    {
      key: "closed",
      label: closedReached
        ? win ? "Closed · WIN" : "Closed · LOSS"
        : "Closed",
      detail: trade
        ? `${trade.exit_reason ?? "—"} · ${fmtPct(Number(trade.net_pnl_rate ?? 0) * 100, true)}`
        : closeSig?.closed_reason ?? undefined,
      ts: trade?.exit_t ?? closeSig?.bar_time,
      reached: closedReached,
      tone: closedReached ? (win ? "win" : "loss") : "ok",
    },
  ];
}

export function SignalTimelinePanel({ tradeId, onClose }: { tradeId: string | null; onClose: () => void }) {
  const { data, isLoading } = useSignalTimeline(tradeId);

  return (
    <div className="fixed inset-0 z-[100] flex items-stretch justify-end bg-background/60 backdrop-blur-sm" onClick={onClose}>
      <div
        className="flex h-full w-full max-w-md flex-col border-l border-border bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-5 py-4">
          <div>
            <div className="text-xs uppercase tracking-widest text-muted-foreground">Signal lifecycle</div>
            <div className="mt-0.5 truncate font-mono text-sm">{tradeId ?? "—"}</div>
          </div>
          <button onClick={onClose} className="rounded-md p-1.5 text-muted-foreground hover:bg-muted/30 hover:text-foreground" aria-label="Close">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-5">
          {!tradeId ? (
            <UnlinkedNotice />
          ) : isLoading || !data ? (
            <div className="text-sm text-muted-foreground">Loading lifecycle…</div>
          ) : (
            <TimelineBody
              signals={data.signals}
              open={data.open}
              trade={data.trade}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function TimelineBody({
  signals, open, trade,
}: { signals: SignalRow[]; open: OpenPositionRow | null; trade: TradeRow | null }) {
  if (signals.length === 0 && !open && !trade) {
    return <div className="text-sm text-muted-foreground">No signals or trade rows found for this id yet.</div>;
  }
  const status = signals[0]
    ? signalStatus(signals[0], { open, trade })
    : open ? "OPEN" : trade ? (Number(trade.net_pnl_rate ?? 0) > 0 ? "CLOSED_WIN" : "CLOSED_LOSS") : "NO_SETUP";

  const stages = buildStages(signals, open, trade);

  return (
    <div>
      <div className="mb-5 flex items-center gap-2">
        <SignalStatusBadge status={status} />
        <span className="text-xs text-muted-foreground">{signals.length} signal{signals.length === 1 ? "" : "s"} on this trade</span>
      </div>
      <ol className="relative space-y-4 border-l border-border pl-5">
        {stages.map((s) => (
          <li key={s.key} className="relative">
            <span
              className={`absolute -left-[27px] grid h-4 w-4 place-items-center rounded-full border ${
                s.reached
                  ? s.tone === "veto"
                    ? "border-destructive/40 bg-destructive/20 text-destructive"
                    : s.tone === "win"
                    ? "border-success/40 bg-success/20 text-success"
                    : s.tone === "loss"
                    ? "border-destructive/40 bg-destructive/20 text-destructive"
                    : "border-primary/40 bg-primary/20 text-primary"
                  : "border-border bg-card text-muted-foreground/50"
              }`}
            >
              {s.reached ? (
                s.tone === "win" ? <ArrowUpRight className="h-2.5 w-2.5" />
                : s.tone === "loss" ? <ArrowDownRight className="h-2.5 w-2.5" />
                : <Check className="h-2.5 w-2.5" />
              ) : (
                <Clock className="h-2.5 w-2.5" />
              )}
            </span>
            <div className={s.reached ? "" : "opacity-40"}>
              <div className="flex items-baseline justify-between gap-3">
                <div className="text-sm font-medium">{s.label}</div>
                <div className="font-mono text-[10px] text-muted-foreground">{s.ts ? fmtAgo(s.ts) : ""}</div>
              </div>
              {s.detail && <div className="mt-0.5 font-mono text-xs text-muted-foreground">{s.detail}</div>}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

function UnlinkedNotice() {
  return (
    <div className="rounded-lg border border-border bg-muted/10 p-4 text-sm text-muted-foreground">
      This signal didn't open a trade — its lifecycle ends at the rule / ML
      stage. Only accepted signals carry a trade id.
    </div>
  );
}
