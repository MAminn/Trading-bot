// Shared sizing constants — the single source of truth for both the Configure
// UI and the server-side validator. Two divergent copies of a table the
// validator enforces is not acceptable in production-live code.

export const SIZING_MODES = ["allocation", "full_capital"] as const;
export type SizingMode = (typeof SIZING_MODES)[number];

// Backward-compatible default. An unknown stored value shows as this.
export const DEFAULT_SIZING_MODE: SizingMode = "allocation";

export function isSizingMode(v: unknown): v is SizingMode {
  return typeof v === "string" && (SIZING_MODES as readonly string[]).includes(v);
}

// In allocation mode the allocation is the single source of truth and leverage
// is derived from it, never free. These are the only ten pairs the UI can
// produce and the only ten the server accepts.
export const LEVERAGE_BY_ALLOC: Record<number, number> = {
  10: 1, 9: 10, 8: 20, 7: 30, 6: 40, 5: 50, 4: 60, 3: 70, 2: 80, 1: 90,
};

export const ALLOC_MIN = 1;
export const ALLOC_MAX = 10;

// full_capital leaves leverage free within the exchange/DB range.
export const LEVERAGE_MIN = 1;
export const LEVERAGE_MAX = 90;

// Capital committed as margin in allocation mode.
export function capitalFromAllocation(accountSizeUsd: number, allocPct: number): number {
  return Math.round(accountSizeUsd * (allocPct / 100));
}

// Liquidation distance, ignoring fees and funding: a 1/leverage adverse move
// wipes out the isolated margin backing the position.
export function liquidationPct(leverage: number): number {
  return leverage > 0 ? 100 / leverage : 0;
}
