// Live-execution control rules — the single implementation of the cross-field
// safety invariants, shared by the Zod validator, the server-function handler
// and the Configure UI.
//
// These mirror the CHECK constraints added in migration
// 20260817130000_engine_config_live_controls.sql. The database is the
// authority; this module exists so a user gets a clear message instead of a
// raw constraint violation, and so the UI can refuse to submit a state the
// server would reject. Three copies of a rule is how they drift, so there is
// exactly one copy here.
//
// Deliberately dependency-free: no framework imports, no path aliases, so it
// can be exercised directly by the Node test runner.

/** Modes a user may request from the web. Deliberately narrower than the
 *  executor's own set — TESTNET_* is a property of the host's environment. */
export const REQUESTED_EXECUTION_MODES = ["OFF", "LIVE_READ", "LIVE_TRADE"] as const;
export type RequestedExecutionMode = (typeof REQUESTED_EXECUTION_MODES)[number];

/** Matches the executor's LIVE_ORDER_CAP_MAX_USD / HARD_CAP_USD / RiskGuard
 *  ABSOLUTE_MAX_NOTIONAL_USD. The DB must not express a cap it would refuse. */
export const LIVE_ORDER_CAP_MAX_USD = 500;

/** ETHUSDT's exchange minimum notional is 20 USDT; after rounding down to the
 *  0.001 step size, a cap below this produces orders rejected as "below min
 *  notional" — a live mode that silently never trades. */
export const LIVE_ORDER_CAP_MIN_TRADE_USD = 25;

/** Every field that participates in the live-execution invariants. A patch
 *  touching any of them must be validated against the resulting merged state,
 *  not against the patch alone. */
export const LIVE_STATE_INPUTS = [
  "execution_mode",
  "live_order_cap_usd",
  "live_allow_full_capital",
  "sizing_mode",
  "demo_mode",
] as const;

/** Columns Phase 1 revoked from `authenticated`. These may only be written
 *  server-side with the service role, after validation. */
export const PRIVILEGED_CONFIG_FIELDS = [
  "execution_mode",
  "live_order_cap_usd",
  "live_allow_full_capital",
  "mode",
] as const;

/** The live-control tuple a patch must declare as a set: validating
 *  `execution_mode` without the cap it will be paired with is not validation. */
export const LIVE_CONTROL_TUPLE = [
  "execution_mode",
  "live_order_cap_usd",
  "live_allow_full_capital",
] as const;

export interface LiveState {
  execution_mode: string;
  live_order_cap_usd: number;
  live_allow_full_capital: boolean;
  sizing_mode: string;
  demo_mode: boolean;
}

export function isRequestedLiveMode(mode: string | null | undefined): boolean {
  return mode === "LIVE_READ" || mode === "LIVE_TRADE";
}

export interface Violation {
  field: keyof LiveState;
  message: string;
}

/**
 * Return every invariant this state violates. An empty array means the state
 * is acceptable.
 *
 * Accepts a partial state and asserts only the rules whose inputs are all
 * present, so it can validate a patch in Zod and the merged row in the
 * handler using the same code. A rule it cannot evaluate is never reported as
 * satisfied — the caller with complete data catches it, and the database
 * catches it regardless.
 */
export function validateLiveState(state: Partial<LiveState>): Violation[] {
  const violations: Violation[] = [];
  const { execution_mode, live_order_cap_usd, live_allow_full_capital, sizing_mode, demo_mode } =
    state;

  if (
    execution_mode !== undefined &&
    !REQUESTED_EXECUTION_MODES.includes(execution_mode as RequestedExecutionMode)
  ) {
    violations.push({
      field: "execution_mode",
      message: `execution_mode must be one of ${REQUESTED_EXECUTION_MODES.join(", ")}`,
    });
    // Every rule below branches on the mode; an unknown one makes them
    // meaningless rather than satisfied.
    return violations;
  }

  if (live_order_cap_usd !== undefined) {
    if (!Number.isFinite(live_order_cap_usd) || live_order_cap_usd < 0) {
      violations.push({
        field: "live_order_cap_usd",
        message: "live order cap must be 0 or greater",
      });
    } else if (live_order_cap_usd > LIVE_ORDER_CAP_MAX_USD) {
      violations.push({
        field: "live_order_cap_usd",
        message: `live order cap must not exceed $${LIVE_ORDER_CAP_MAX_USD} — the executor refuses anything higher`,
      });
    }
  }

  if (execution_mode === "LIVE_TRADE" && live_order_cap_usd !== undefined) {
    if (live_order_cap_usd < LIVE_ORDER_CAP_MIN_TRADE_USD) {
      violations.push({
        field: "live_order_cap_usd",
        message:
          `LIVE_TRADE requires a live order cap of at least $${LIVE_ORDER_CAP_MIN_TRADE_USD} ` +
          `— below that every order is rejected as under the exchange minimum notional`,
      });
    }
  }

  if (
    execution_mode === "LIVE_TRADE" &&
    sizing_mode === "full_capital" &&
    live_allow_full_capital !== undefined &&
    !live_allow_full_capital
  ) {
    violations.push({
      field: "live_allow_full_capital",
      message:
        "full-capital sizing on a live-trading config requires the explicit " +
        "full-capital consent toggle",
    });
  }

  if (execution_mode !== undefined && demo_mode === true && execution_mode !== "OFF") {
    violations.push({
      field: "demo_mode",
      message:
        "demo mode fabricates signals and cannot be enabled while execution mode " +
        "is LIVE_READ or LIVE_TRADE — turn demo mode off first",
    });
  }

  return violations;
}

/** Violations rendered as one message, for a thrown Error or a toast. */
export function formatViolations(violations: Violation[]): string {
  return violations.map((v) => v.message).join("; ");
}
