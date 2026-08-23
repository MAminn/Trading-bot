// What the UI is allowed to say about the executor's relationship to THIS user.
//
// Deliberately dependency-free, for the same reason as wallet-display.ts: this
// is the copy a client reads to decide whether the product is real, so it lives
// somewhere it can be tested directly and read in one sitting.
//
// The bug it exists to prevent: Configure carried a banner announcing that the
// page was not connected to the executor, that these settings were saved and
// validated but never read, and that the executor still followed its own VPS
// environment. That was true of the single-tenant executor, which took one
// ENGINE_USER_ID from its environment. It stopped being true with multi-tenant
// onboarding: the executor now polls
// /api/public/engine/users/active, reads each user's execution config from the
// database every cycle, and fetches that user's own Binance credentials. Left
// in production the banner tells a paying client their bot is a mock-up.
//
// The rule: the page may state that the executor READS this user's settings —
// that is now structurally true for everyone on the roster — but it may never
// state that the executor is holding, or has read, this user's MONEY until a
// signed Binance read has actually come back. Those two claims are separated
// here so one cannot drift into the other.

import { resolveWalletDisplay, type WalletSource } from "./wallet-display.ts";

// ----- The settings claim -----
//
// True for every user on the execution roster, independent of keys, balances or
// telemetry. It describes the executor's architecture, not this user's wallet.

export const EXECUTOR_CONNECTED_TITLE = "Connected to multi-tenant executor";

export const EXECUTOR_READS_SETTINGS =
  "The executor reads this user's live settings from the database each cycle.";

/** Every gate that stands between a saved setting and a real order. Kept as one
 *  sentence, in the order the executor evaluates them, so no page can advertise
 *  a subset and imply the rest are already satisfied. */
export const EXECUTOR_LIVE_ORDER_REQUIREMENTS =
  "Live orders still require connected Binance keys, Start enabled, LIVE_TRADE " +
  "selected, auto-execute enabled, host LIVE_TRADE ceiling, ACK, and live order cap.";

// ----- The wallet claim -----

export type ExecutorLinkState =
  /** No Binance account is linked to this user. */
  | "not_connected"
  /** Keys are connected; no signed balance read has come back yet. */
  | "awaiting_read"
  /** The executor has read this user's Binance wallet with their keys. */
  | "linked";

/** The one wording each state gets, everywhere. Configure, Engine and Dashboard
 *  all render these strings so a client moving between pages is never told two
 *  different things about the same connection. */
export const EXECUTOR_LINK_LABEL: Record<ExecutorLinkState, string> = {
  not_connected: "Connect Binance first",
  awaiting_read: "Waiting for first executor read",
  linked: "Executor linked to your Binance wallet",
};

/** The supporting line under each label. None of them names a figure — the
 *  balance itself is resolved separately, by resolveWalletDisplay. */
export const EXECUTOR_LINK_HINT: Record<ExecutorLinkState, string> = {
  not_connected:
    "No Binance keys are connected for this account, so the executor has nothing to trade with.",
  awaiting_read:
    "Your keys are stored. The executor picks them up on its next cycle and reports the balance it reads.",
  linked: "Balances and positions shown for this account come from signed reads with your keys.",
};

/**
 * Which of the three things the UI may say about this user's exchange link.
 *
 * Delegates entirely to resolveWalletDisplay so there is exactly one rule
 * deciding whether a signed Binance read exists. In particular `linked` requires
 * an actual balance to have come back — `keys_present: true` on its own is the
 * executor saying it HAS the keys, not that it has used them, and promoting that
 * to "linked to your wallet" would be the same borrowed-confidence mistake in a
 * new place.
 *
 * @param row      the user's executor_status telemetry
 * @param options  `keysConnected` from the user's own key metadata, used when
 *                 the executor has not reported yet
 */
export function resolveExecutorLink(
  row: WalletSource,
  options?: { keysConnected?: boolean | null },
  now: number = Date.now(),
): ExecutorLinkState {
  const wallet = resolveWalletDisplay(row, options, now);
  if (wallet.state === "connected") return "linked";
  if (wallet.state === "awaiting_read") return "awaiting_read";
  return "not_connected";
}
