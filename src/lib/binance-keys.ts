// The signed-in user's Binance key metadata.
//
// Deliberately routed through getBinanceKeyInfo rather than through the
// executor's telemetry: that server function is RLS-scoped to the signed-in user
// and returns last4 plus timestamps, so a page cannot render a secret even if a
// later change tried to. The plaintext key never exists in the browser at all —
// the one place it is decrypted is the executor's credentials endpoint,
// server-side, and that response never reaches a page.
//
// Shared because three pages need the same answer to "has this client connected
// a wallet at all": Configure, Engine and Dashboard. Asking it three different
// ways is how they come to disagree in front of the client.

import { useQuery } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { getBinanceKeyInfo } from "./binance.functions";

export const BINANCE_KEY_INFO_KEY = ["binance", "key-info"] as const;

export function useBinanceKeyInfo() {
  const fetchInfo = useServerFn(getBinanceKeyInfo);
  return useQuery({
    queryKey: BINANCE_KEY_INFO_KEY,
    queryFn: () => fetchInfo(),
    staleTime: 60_000,
  });
}

/**
 * Has this user connected Binance keys?
 *
 * `undefined` while the query is in flight — a distinct answer from `false`, so
 * a page can hold its copy back rather than flash the not-connected wording at
 * a client who connected months ago.
 */
export function keysConnected(query: {
  data?: { api_key_last4: string } | null;
  isLoading: boolean;
}): boolean | undefined {
  if (query.isLoading) return undefined;
  return !!query.data;
}
