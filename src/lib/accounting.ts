// Real Binance accounting data access.
//
// The third layer of the trade record, and the only one that is the customer's
// money:
//
//   lib/engine.ts    user_trades      what the STRATEGY did (modelled: a
//                                     fractional return x the capital_usd
//                                     config column)
//   lib/executor.ts  engine_orders    what the EXECUTOR sent to Binance
//   lib/accounting   executed_trades  what BINANCE charged and paid (this file)
//
// Kept as its own module rather than folded into lib/engine.ts so that nothing
// in the trading path imports it and nothing here can be mistaken for a
// strategy figure. Read-only: there is no mutation in this file. Rows are
// written solely by the read-only accounting synchroniser through a
// service-role endpoint, and RLS scopes every read below to the signed-in user.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { supabase } from "@/integrations/supabase/client";
import type { ExecutedTradeRow } from "./accounting-math";

const POLL = 15_000;

export const EXECUTED_TRADES_KEY = ["accounting", "executed_trades"] as const;

/**
 * This user's real completed Binance trades, newest first.
 *
 * RLS restricts the result to `auth.uid() = user_id`; the service role writes
 * every customer's rows into one table and no client can read another's.
 */
export function useExecutedTrades(limit = 500) {
  return useQuery({
    queryKey: [...EXECUTED_TRADES_KEY, limit],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("executed_trades")
        .select("*")
        .order("exit_time", { ascending: false })
        .limit(limit);
      // The table may not exist yet on an app deployed ahead of its migration.
      // That is "no real accounting yet", not a page-breaking error — and it
      // renders as an explicit empty state, never as zeros.
      if (error) return [] as ExecutedTradeRow[];
      return (data ?? []) as unknown as ExecutedTradeRow[];
    },
    refetchInterval: POLL,
  });
}

/** Invalidate the accounting feed when the synchroniser writes a new trade. */
export function useAccountingRealtime() {
  const qc = useQueryClient();
  useEffect(() => {
    const channel = supabase
      .channel("accounting-feed")
      .on("postgres_changes", { event: "*", schema: "public", table: "executed_trades" }, () =>
        qc.invalidateQueries({ queryKey: EXECUTED_TRADES_KEY }),
      )
      .subscribe();
    return () => {
      supabase.removeChannel(channel);
    };
  }, [qc]);
}

// The arithmetic lives in accounting-math.ts, which imports nothing, so it can
// be tested directly without a Supabase client. Re-exported here because pages
// read everything accounting-related from this module.
export {
  isComplete,
  realPerformance,
  realTotalsForDay,
  closedOn,
  realTradesCsv,
  REAL_TRADE_CSV_HEADER,
  fmtCommission,
  netTone,
  incompleteLabel,
  closeSourceLabel,
  UNAVAILABLE,
  EMPTY_REAL_PERFORMANCE,
} from "./accounting-math";
export type { ExecutedTradeRow, RealPerformance } from "./accounting-math";
