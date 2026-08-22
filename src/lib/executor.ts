// Executor telemetry: what the Binance executor is ACTUALLY doing.
//
// Deliberately separate from engine_status, which the ML signal worker writes.
// engine_status reports the strategy's paper position; this reports the
// exchange's real one, the real execution mode, and real balances. The two
// disagree routinely and both are correct — they describe different things.
//
// Read-only. Nothing here controls the executor: its live capability comes
// from its own environment, not from the database.

import { useQuery } from "@tanstack/react-query";
import { supabase } from "@/integrations/supabase/client";
import { heartbeatFresh } from "./engine";

const POLL = 10_000;

export const EXECUTOR_STATUS_KEY = ["executor", "status"] as const;

export const EXECUTION_MODES = [
  "OFF",
  "TESTNET_READ",
  "TESTNET_TRADE",
  "LIVE_READ",
  "LIVE_TRADE",
] as const;
export type ExecutionMode = (typeof EXECUTION_MODES)[number];

export interface ExecutorStatusRow {
  user_id: string;
  // Three separate facts. `db_execution_mode` is what was asked for,
  // `env_mode_ceiling` is what the host permits, `effective_mode` is what is
  // running. They differ whenever a request is degraded, and the UI must show
  // that rather than picking one.
  effective_mode: ExecutionMode;
  env_mode_ceiling: ExecutionMode | null;
  db_execution_mode: "OFF" | "LIVE_READ" | "LIVE_TRADE" | null;
  auto_execute_enabled: boolean | null;
  live_order_cap_usd: number | null;
  live_order_cap_env_max: number | null;
  orders_enabled: boolean | null;
  blocked_reason: string | null;
  wallet_balance_usd: number | null;
  available_balance_usd: number | null;
  position_amt: number | null;
  position_side: "FLAT" | "LONG" | "SHORT" | null;
  entry_price: number | null;
  position_leverage: number | null;
  margin_type: string | null;
  reconcile_match: boolean | null;
  reconcile_expected: number | null;
  reconcile_actual: number | null;
  last_reconcile_at: string | null;
  keys_present: boolean | null;
  permission_status: "verified_futures" | "unknown" | "failed" | null;
  message: string | null;
  last_heartbeat: string | null;
  updated_at: string | null;
}

/** One row of engine_orders: what the EXECUTOR did about a strategy signal.
 *
 *  Distinct from user_trades, which is what the STRATEGY did. The two are
 *  deliberately never merged: a strategy signal that was never sent to Binance,
 *  an order that was sent and rejected, and an order that filled are three
 *  different facts, and collapsing them is how a client comes to believe a
 *  paper result was a real one.
 */
export interface EngineOrderRow {
  id: string;
  signal_bar_time: string;
  symbol: string | null;
  side: "LONG" | "SHORT";
  intent: "OPEN" | "CLOSE";
  qty: number | null;
  ref_price: number | null;
  notional_usd: number | null;
  execution_mode: string | null;
  /** INTENT_LOGGED | DRYRUN | SENT | FILLED | FAILED | SKIPPED */
  status: string;
  binance_order_id: string | null;
  error: string | null;
  created_at: string;
}

/** Which order states mean an order actually reached Binance. */
export const ORDER_REACHED_EXCHANGE = ["SENT", "FILLED"] as const;

/** Which order states mean real money actually moved. */
export const ORDER_FILLED = ["FILLED"] as const;

/**
 * Plain-language meaning of each engine_orders.status.
 *
 * Written for a client reading their own history, not for an operator: the
 * distinction that matters to them is "did this actually happen on my Binance
 * account", and every label answers that first.
 */
export const ORDER_STATUS_LABEL: Record<string, string> = {
  INTENT_LOGGED: "Not sent — recorded only",
  DRYRUN: "Not sent — dry run",
  SKIPPED: "Not sent — blocked",
  SENT: "Sent to Binance",
  FILLED: "Filled on Binance",
  FAILED: "Rejected by Binance",
};

export function useEngineOrders(limit = 200) {
  return useQuery({
    queryKey: ["engine", "orders", limit],
    queryFn: async () => {
      // RLS scopes this to the signed-in user; one client can never read
      // another's orders even though the executor writes everyone's.
      const { data, error } = await supabase
        .from("engine_orders")
        .select("*")
        .order("created_at", { ascending: false })
        .limit(limit);
      // The table may not exist yet on an app deployed ahead of its migration.
      if (error) return [] as EngineOrderRow[];
      return (data ?? []) as unknown as EngineOrderRow[];
    },
    refetchInterval: POLL,
  });
}

export function useExecutorStatus() {
  return useQuery({
    queryKey: EXECUTOR_STATUS_KEY,
    queryFn: async () => {
      const { data, error } = await supabase.from("executor_status").select("*").maybeSingle();
      // The table may not exist yet on an app deployed ahead of its migration.
      // That is a "no telemetry" state, not a page-breaking error.
      if (error) return null;
      return (data as unknown as ExecutorStatusRow | null) ?? null;
    },
    refetchInterval: POLL,
  });
}

// ----- Mode semantics -----

/** Modes that touch mainnet with real funds. */
export function isLiveMode(mode: ExecutionMode | null | undefined): boolean {
  return mode === "LIVE_READ" || mode === "LIVE_TRADE";
}

/** The only mode that can place an order with real money. */
export function isLiveTrading(mode: ExecutionMode | null | undefined): boolean {
  return mode === "LIVE_TRADE";
}

/** Modes that can place an order at all, on either network. */
export function canPlaceOrders(mode: ExecutionMode | null | undefined): boolean {
  return mode === "LIVE_TRADE" || mode === "TESTNET_TRADE";
}

export const MODE_LABEL: Record<ExecutionMode, string> = {
  OFF: "Executor off",
  TESTNET_READ: "Testnet · read-only",
  TESTNET_TRADE: "Testnet trading",
  LIVE_READ: "Live · read-only",
  LIVE_TRADE: "Live trading",
};

export type ModeTone = "live" | "warn" | "muted";

export const MODE_TONE: Record<ExecutionMode, ModeTone> = {
  OFF: "muted",
  TESTNET_READ: "muted",
  TESTNET_TRADE: "warn",
  LIVE_READ: "warn",
  LIVE_TRADE: "live",
};

/**
 * The label for the app header / dashboard title.
 *
 * Once the executor has ever reported, its mode wins outright — including when
 * the heartbeat is stale. A stale heartbeat means we do not know what the
 * executor is doing, and "we lost contact with a LIVE_TRADE executor" must
 * never render as the reassuring "Signals only". Staleness is surfaced
 * alongside the label, not by replacing it.
 *
 * `configMode` is the fallback used only when no executor has ever reported.
 */
export function executionModeLabel(
  executor: ExecutorStatusRow | null | undefined,
  configMode: string | null | undefined,
): string {
  if (executor) return MODE_LABEL[executor.effective_mode] ?? executor.effective_mode;
  return configMode === "auto" ? "Auto" : "Signals only";
}

export function executorTone(executor: ExecutorStatusRow | null | undefined): ModeTone {
  if (!executor) return "muted";
  return MODE_TONE[executor.effective_mode] ?? "muted";
}

/** True when the executor's heartbeat is recent enough to trust the snapshot. */
export function executorFresh(executor: ExecutorStatusRow | null | undefined): boolean {
  return heartbeatFresh(executor?.last_heartbeat);
}

export function positionSideLabel(row: ExecutorStatusRow | null | undefined): string {
  if (!row || row.position_side === null) return "—";
  return row.position_side;
}

export const PERMISSION_LABEL: Record<string, string> = {
  verified_futures: "Futures access verified",
  unknown: "Not verified",
  failed: "Rejected by Binance",
};
