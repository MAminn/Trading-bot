// Per-user engine data hooks. The actual Python engine runs externally and
// writes rows into user_signals, user_trades, engine_status via service-role
// endpoints. The UI only reads/displays and writes user commands (Start/Stop,
// config) via server functions.
//
// When the engine is stopped or there is no data yet, hooks return empty
// arrays / nulls — pages must render an explicit empty state, never fake data.

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { supabase } from "@/integrations/supabase/client";
import { useServerFn } from "@tanstack/react-start";
import {
  setEngineRunning as setEngineRunningFn,
  updateEngineConfig as updateEngineConfigFn,
  closePosition as closePositionFn,
  closeAllPositions as closeAllPositionsFn,
} from "./engine.functions";

const POLL = 10_000;

// ----- Types -----

export type EngineStatusKind = "running" | "stopped" | "error";
export type EnginePosition = "FLAT" | "LONG" | "SHORT";
export type EngineMode = "signal_only" | "auto";

export interface EngineStatusRow {
  user_id: string;
  status: EngineStatusKind;
  last_heartbeat: string | null;
  current_position: EnginePosition;
  message: string | null;
  updated_at: string | null;
}

export interface EngineConfigRow {
  user_id: string;
  mode: EngineMode;
  capital_usd: number;
  capital_allocation_pct: number;
  leverage: number;
  max_daily_loss_usd: number;
  max_position_size_usd: number;
  is_running: boolean;
  demo_mode: boolean;
  updated_at: string | null;
}

export interface OpenPositionRow {
  id: string;
  user_id: string;
  trade_id: string;
  side: string | null;
  setup_name: string | null;
  entry_t: string | null;
  entry: number | null;
  sl: number | null;
  tp: number | null;
  current_stop: number | null;
  atr: number | null;
  bars_held: number | null;
  prob: number | null;
  threshold: number | null;
  unrealized_pnl_rate: number | null;
  updated_at: string | null;
}

export interface SignalRow {
  id: string;
  user_id: string;
  bar_time: string;
  bar_closed_now: boolean | null;
  valid_next_entry: boolean | null;
  rule_side: number | null;
  rule_reason: string | null;
  ml_prob: number | null;
  ml_threshold: number | null;
  ml_accept: boolean | null;
  opened: string | null;
  closed_reason: string | null;
  position_before: string | null;
  position_after: string | null;
  trade_id: string | null;
  created_at: string;
}

export interface TradeRow {
  id: string;
  user_id: string;
  trade_id: string | null;
  side: string | null;
  setup_name: string | null;
  signal_t: string | null;
  entry_t: string | null;
  exit_t: string | null;
  entry: number | null;
  exit: number | null;
  tp: number | null;
  sl: number | null;
  final_stop: number | null;
  atr: number | null;
  bars_held: number | null;
  prob: number | null;
  threshold: number | null;
  exit_reason: string | null;
  net_pnl_rate: number | null; // fractional return (e.g. 0.012 = +1.2%)
  round_trip_cost: number | null;
  created_at: string;
}

// ----- Health helpers -----

const HEARTBEAT_FRESH_MS = 3 * 60_000; // 3 minutes

export function heartbeatFresh(ts: string | null | undefined) {
  if (!ts) return false;
  return Date.now() - new Date(ts).getTime() < HEARTBEAT_FRESH_MS;
}

export type LiveState = "running" | "starting" | "stale" | "error" | "stopped";

export function liveState(
  s: EngineStatusRow | null | undefined,
  isRunning?: boolean,
): LiveState {
  if (!s) return isRunning ? "starting" : "stopped";
  if (s.status === "error") return "error";
  if (s.status === "running" && heartbeatFresh(s.last_heartbeat)) return "running";
  if (isRunning) return "starting";
  if (s.status === "running") return "stale";
  return "stopped";
}

// ----- Queries -----

export function useEngineStatus() {
  return useQuery({
    queryKey: ["engine", "status"],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("engine_status")
        .select("*")
        .maybeSingle();
      if (error) throw error;
      return (data as EngineStatusRow | null) ?? null;
    },
    refetchInterval: POLL,
  });
}

export function useEngineConfig() {
  return useQuery({
    queryKey: ["engine", "config"],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("engine_config")
        .select("*")
        .maybeSingle();
      if (error) throw error;
      return (data as EngineConfigRow | null) ?? null;
    },
    refetchInterval: POLL,
  });
}

export function useSignals(limit = 50) {
  return useQuery({
    queryKey: ["engine", "signals", limit],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("user_signals")
        .select("*")
        .order("created_at", { ascending: false })
        .limit(limit);
      if (error) throw error;
      return (data ?? []) as SignalRow[];
    },
    refetchInterval: POLL,
  });
}

export function useTrades(limit = 500) {
  return useQuery({
    queryKey: ["engine", "trades", limit],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("user_trades")
        .select("*")
        .order("exit_t", { ascending: false, nullsFirst: false })
        .limit(limit);
      if (error) throw error;
      return (data ?? []) as TradeRow[];
    },
    refetchInterval: POLL,
  });
}

export function useOpenPositions() {
  return useQuery({
    queryKey: ["engine", "open_positions"],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("open_positions")
        .select("*")
        .order("entry_t", { ascending: false, nullsFirst: false });
      if (error) throw error;
      return (data ?? []) as OpenPositionRow[];
    },
    refetchInterval: POLL,
  });
}

// ----- Realtime: invalidate on insert/update -----

export function useEngineRealtime() {
  const qc = useQueryClient();
  useEffect(() => {
    const channel = supabase
      .channel("engine-feed")
      .on("postgres_changes", { event: "*", schema: "public", table: "engine_status" },
        () => qc.invalidateQueries({ queryKey: ["engine", "status"] }))
      .on("postgres_changes", { event: "*", schema: "public", table: "engine_config" },
        () => qc.invalidateQueries({ queryKey: ["engine", "config"] }))
      .on("postgres_changes", { event: "INSERT", schema: "public", table: "user_signals" },
        () => qc.invalidateQueries({ queryKey: ["engine", "signals"] }))
      .on("postgres_changes", { event: "INSERT", schema: "public", table: "user_trades" },
        () => qc.invalidateQueries({ queryKey: ["engine", "trades"] }))
      .on("postgres_changes", { event: "*", schema: "public", table: "open_positions" },
        () => qc.invalidateQueries({ queryKey: ["engine", "open_positions"] }))
      .subscribe();
    return () => {
      supabase.removeChannel(channel);
    };
  }, [qc]);
}

// ----- Mutations -----

export function useSetRunning() {
  const qc = useQueryClient();
  const fn = useServerFn(setEngineRunningFn);
  return useMutation({
    mutationFn: (running: boolean) => fn({ data: { running } }),
    onSuccess: () => {
      // Refetch both config (is_running flag) and status (the "starting…"
      // row the server upserts) so the pill flips immediately, instead of
      // waiting up to 10s for the next poll tick.
      qc.invalidateQueries({ queryKey: ["engine", "config"] });
      qc.invalidateQueries({ queryKey: ["engine", "status"] });
    },
  });
}

export function useUpdateConfig() {
  const qc = useQueryClient();
  const fn = useServerFn(updateEngineConfigFn);
  return useMutation({
    mutationFn: (patch: Partial<Pick<EngineConfigRow, "capital_usd" | "capital_allocation_pct" | "leverage" | "max_daily_loss_usd" | "max_position_size_usd" | "mode" | "demo_mode">>) =>
      fn({ data: patch }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["engine", "config"] }),
  });
}

export function useClosePosition() {
  const qc = useQueryClient();
  const fn = useServerFn(closePositionFn);
  return useMutation({
    mutationFn: (id: string) => fn({ data: { id } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["engine", "open_positions"] });
      qc.invalidateQueries({ queryKey: ["engine", "trades"] });
      qc.invalidateQueries({ queryKey: ["engine", "signals"] });
    },
  });
}

export function useCloseAllPositions() {
  const qc = useQueryClient();
  const fn = useServerFn(closeAllPositionsFn);
  return useMutation({
    mutationFn: () => fn({}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["engine", "open_positions"] });
      qc.invalidateQueries({ queryKey: ["engine", "trades"] });
      qc.invalidateQueries({ queryKey: ["engine", "signals"] });
    },
  });
}

// ----- Metrics -----

export interface EngineMetrics {
  totalTrades: number;
  wins: number;
  losses: number;
  winRate: number;
  profitFactor: number;
  netPnl: number;
  avgWin: number;
  avgLoss: number;
  maxDrawdown: number; // % of peak
  equityCurve: { t: number; iso: string; v: number }[];
}

/**
 * Compute equity / KPIs from per-user trades.
 * `net_pnl_rate` is a fractional return; multiply by starting capital to get $ P&L.
 */
export function computeMetrics(trades: TradeRow[], startingCapital = 10_000): EngineMetrics {
  const sorted = [...trades]
    .filter((t) => t.exit_t && t.net_pnl_rate != null)
    .sort((a, b) => new Date(a.exit_t!).getTime() - new Date(b.exit_t!).getTime());

  let equity = startingCapital;
  let peak = startingCapital;
  let maxDD = 0;
  let wins = 0;
  let losses = 0;
  let grossWin = 0;
  let grossLoss = 0;
  const curve: { t: number; iso: string; v: number }[] = [];

  sorted.forEach((t, i) => {
    const pnl = Number(t.net_pnl_rate) * startingCapital;
    equity += pnl;
    if (pnl > 0) { wins++; grossWin += pnl; }
    else if (pnl < 0) { losses++; grossLoss += Math.abs(pnl); }
    peak = Math.max(peak, equity);
    const dd = ((equity - peak) / peak) * 100;
    if (dd < maxDD) maxDD = dd;
    curve.push({ t: i, iso: t.exit_t!, v: Math.round(equity * 100) / 100 });
  });

  const total = sorted.length;
  return {
    totalTrades: total,
    wins,
    losses,
    winRate: total ? (wins / total) * 100 : 0,
    profitFactor: grossLoss > 0 ? grossWin / grossLoss : grossWin > 0 ? Infinity : 0,
    netPnl: equity - startingCapital,
    avgWin: wins ? grossWin / wins : 0,
    avgLoss: losses ? -grossLoss / losses : 0,
    maxDrawdown: maxDD,
    equityCurve: curve,
  };
}

export function tradePnlUsd(t: TradeRow, capital: number) {
  return Number(t.net_pnl_rate ?? 0) * capital;
}

export function signalSideLabel(s: SignalRow): "LONG" | "SHORT" | "FLAT" {
  if (s.rule_side === 1) return "LONG";
  if (s.rule_side === -1) return "SHORT";
  return "FLAT";
}

// ----- Signal lifecycle status -----

export type SignalStatus =
  | "NO_SETUP"
  | "REJECTED"
  | "ACCEPTED"
  | "OPEN"
  | "CLOSED_WIN"
  | "CLOSED_LOSS";

/**
 * Derive lifecycle status from a signal row plus (optional) the related
 * open_position and closed trade looked up by trade_id. The dashboard,
 * history, and timeline all use this to render a single source of truth.
 */
export function signalStatus(
  s: SignalRow,
  related?: { open?: OpenPositionRow | null; trade?: TradeRow | null },
): SignalStatus {
  const trade = related?.trade ?? null;
  const open = related?.open ?? null;
  if (trade) return Number(trade.net_pnl_rate ?? 0) > 0 ? "CLOSED_WIN" : "CLOSED_LOSS";
  if (open) return "OPEN";
  const side = signalSideLabel(s);
  if (side === "FLAT") return "NO_SETUP";
  if (s.ml_accept === false) return "REJECTED";
  // Side was set and ML didn't reject — treat as accepted even if not yet linked
  if (s.opened || s.trade_id) return "ACCEPTED";
  if (s.ml_accept === true) return "ACCEPTED";
  return "NO_SETUP";
}

export const STATUS_META: Record<SignalStatus, { label: string; cls: string }> = {
  NO_SETUP:    { label: "NO SETUP",   cls: "bg-muted/30 text-muted-foreground" },
  REJECTED:    { label: "ML VETO",    cls: "bg-destructive/10 text-destructive/80" },
  ACCEPTED:    { label: "ACCEPTED",   cls: "bg-primary/15 text-primary" },
  OPEN:        { label: "OPEN",       cls: "bg-warning/20 text-warning" },
  CLOSED_WIN:  { label: "WIN",        cls: "bg-success/15 text-success" },
  CLOSED_LOSS: { label: "LOSS",       cls: "bg-destructive/15 text-destructive" },
};

/**
 * Fetch every signal + the open position + the closed trade that share a
 * trade_id, so the timeline panel can render the full lifecycle for one trade.
 */
export function useSignalTimeline(tradeId: string | null | undefined) {
  return useQuery({
    queryKey: ["engine", "timeline", tradeId],
    enabled: !!tradeId,
    queryFn: async () => {
      const [sigs, opens, trades] = await Promise.all([
        supabase.from("user_signals").select("*").eq("trade_id", tradeId!).order("created_at", { ascending: true }),
        supabase.from("open_positions").select("*").eq("trade_id", tradeId!).maybeSingle(),
        supabase.from("user_trades").select("*").eq("trade_id", tradeId!).maybeSingle(),
      ]);
      if (sigs.error) throw sigs.error;
      return {
        signals: (sigs.data ?? []) as SignalRow[],
        open: (opens.data as OpenPositionRow | null) ?? null,
        trade: (trades.data as TradeRow | null) ?? null,
      };
    },
    refetchInterval: POLL,
  });
}

// ----- Formatting -----

export function fmtUSD(n: number, signed = false) {
  const sign = signed && n > 0 ? "+" : n < 0 ? "−" : "";
  return `${sign}$${Math.abs(n).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function fmtPct(n: number, signed = false) {
  if (!Number.isFinite(n)) return "—";
  const sign = signed && n > 0 ? "+" : n < 0 ? "−" : "";
  return `${sign}${Math.abs(n).toFixed(2)}%`;
}

export function fmtAgo(ts: string | null | undefined) {
  if (!ts) return "never";
  const s = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
