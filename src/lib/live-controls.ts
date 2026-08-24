// Live-execution control rules — the single implementation of the cross-field
// safety invariants, shared by the Zod validator, the server-function handler
// and the Configure UI.
//
// These mirror the CHECK constraints added in migration
// 20260817130000_engine_config_live_controls.sql and amended in
// 20260824120000_single_sizing_model.sql. The database is the authority; this
// module exists so a user gets a clear message instead of a raw constraint
// violation, and so the UI can refuse to submit a state the server would reject.
// Three copies of a rule is how they drift, so there is exactly one copy here.
//
// Deliberately dependency-free: no framework imports, no path aliases, so it
// can be exercised directly by the Node test runner.

/** Modes a user may request from the web. Deliberately narrower than the
 *  executor's own set — TESTNET_* is a property of the host's environment. */
export const REQUESTED_EXECUTION_MODES = ["OFF", "LIVE_READ", "LIVE_TRADE"] as const;
export type RequestedExecutionMode = (typeof REQUESTED_EXECUTION_MODES)[number];

/** Every field that participates in the live-execution invariants. A patch
 *  touching any of them must be validated against the resulting merged state,
 *  not against the patch alone. */
export const LIVE_STATE_INPUTS = ["execution_mode", "demo_mode"] as const;

/** Columns Phase 1 revoked from `authenticated`. These may only be written
 *  server-side with the service role, after validation. */
export const PRIVILEGED_CONFIG_FIELDS = ["execution_mode", "mode"] as const;

export interface LiveState {
  execution_mode: string;
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
  const { execution_mode, demo_mode } = state;

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
