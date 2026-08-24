// Shared sizing constants — the single source of truth for the Configure UI,
// the server-side validator and the database CHECK constraints. Two divergent
// copies of a table the validator enforces is not acceptable in production-live
// code.
//
// There is exactly ONE sizing model:
//
//     target notional = Binance USD-M wallet balance
//                     x allocation %
//                     x leverage
//
// The wallet balance is read from the authenticated user's own Binance Futures
// account (`totalWalletBalance`) by the executor on every sizing decision. It is
// never entered by hand, never stored, and never taken from another user.
// Allocation and leverage are two INDEPENDENT controls: moving one never moves
// the other.

/** Percentage of the real Binance Futures wallet balance committed as margin.
 *  Exactly these values are selectable, persisted and accepted.
 *
 *  1% is a deliberate special first step — the smallest meaningful commitment,
 *  below the regular grid. From 5% onward the scale is exact 5% increments
 *  through 100%. This is the ONE list; the Configure selector, the Zod
 *  validator and the database CHECK all derive from it, so none of them can
 *  drift into permitting a value another layer refuses. */
export const ALLOCATION_PCTS = [
  1,
  5, 10, 15, 20, 25, 30, 35, 40, 45, 50,
  55, 60, 65, 70, 75, 80, 85, 90, 95, 100,
] as const;
export type AllocationPct = (typeof ALLOCATION_PCTS)[number];

/** Leverage multipliers. Independent of allocation — this table is NOT indexed
 *  by, derived from, or paired with an allocation value. */
export const LEVERAGE_STEPS = [1, 10, 20, 30, 40, 50, 60, 70, 80, 90] as const;
export type LeverageStep = (typeof LEVERAGE_STEPS)[number];

/** Fail-small defaults: an unreadable stored value shows as the smallest
 *  selectable one rather than as the largest. */
export const DEFAULT_ALLOCATION_PCT: AllocationPct = 1;
export const DEFAULT_LEVERAGE: LeverageStep = 1;

export const ALLOC_MIN = ALLOCATION_PCTS[0];
export const ALLOC_MAX = ALLOCATION_PCTS[ALLOCATION_PCTS.length - 1];
export const LEVERAGE_MIN = LEVERAGE_STEPS[0];
export const LEVERAGE_MAX = LEVERAGE_STEPS[LEVERAGE_STEPS.length - 1];

export function isAllocationPct(v: unknown): v is AllocationPct {
  return typeof v === "number" && (ALLOCATION_PCTS as readonly number[]).includes(v);
}

export function isLeverageStep(v: unknown): v is LeverageStep {
  return typeof v === "number" && (LEVERAGE_STEPS as readonly number[]).includes(v);
}

/** Snap an arbitrary stored number DOWN to the nearest selectable value.
 *  Downwards on purpose: a legacy row that cannot be represented exactly must
 *  round to less exposure, never more. */
function snapDown<T extends number>(steps: readonly T[], v: unknown, fallback: T): T {
  const n = Number(v);
  if (!Number.isFinite(n)) return fallback;
  let best = fallback;
  for (const s of steps) if (s <= n) best = s;
  return best;
}

export function toAllocationPct(v: unknown): AllocationPct {
  return snapDown(ALLOCATION_PCTS, v, DEFAULT_ALLOCATION_PCT);
}

export function toLeverageStep(v: unknown): LeverageStep {
  return snapDown(LEVERAGE_STEPS, v, DEFAULT_LEVERAGE);
}

/** Margin committed: a percentage of the REAL wallet balance. Not rounded —
 *  the executor computes the same product in Decimal and the two must agree. */
export function allocatedMargin(walletUsd: number, allocPct: number): number {
  if (!Number.isFinite(walletUsd) || walletUsd <= 0) return 0;
  return walletUsd * (allocPct / 100);
}

/** The position size the executor aims for, before exchange bracket limits and
 *  the operator's live order cap. */
export function targetNotional(walletUsd: number, allocPct: number, leverage: number): number {
  return allocatedMargin(walletUsd, allocPct) * leverage;
}

// Liquidation distance, ignoring fees and funding: a 1/leverage adverse move
// wipes out the isolated margin backing the position.
export function liquidationPct(leverage: number): number {
  return leverage > 0 ? 100 / leverage : 0;
}
