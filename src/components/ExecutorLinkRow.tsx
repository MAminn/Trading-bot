// One client, one sentence about their exchange link — whichever page they are on.
//
// Configure, Engine and Dashboard all render this component rather than each
// paraphrasing the same three states. The wording itself lives in
// lib/executor-link.ts, where it is tested; this file is only how it looks.

import { Link } from "@tanstack/react-router";
import { Hourglass, Link2, Link2Off, Loader2 } from "lucide-react";
import {
  EXECUTOR_LINK_HINT,
  EXECUTOR_LINK_LABEL,
  type ExecutorLinkState,
} from "@/lib/executor-link";

export function ExecutorLinkRow({
  link,
  pending,
}: {
  link: ExecutorLinkState;
  /** Neither the key metadata nor the executor telemetry has answered yet.
   *  Showing the not-connected wording to a client who connected months ago,
   *  for the half-second before the query lands, is exactly the kind of false
   *  statement this component exists to stop. */
  pending?: boolean;
}) {
  if (pending) {
    return (
      <div className="flex items-start gap-2 rounded-lg border border-border bg-card/40 p-3 text-xs text-muted-foreground">
        <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin" />
        <span>Checking your Binance connection…</span>
      </div>
    );
  }

  const Icon = link === "linked" ? Link2 : link === "awaiting_read" ? Hourglass : Link2Off;
  const tone =
    link === "linked"
      ? "border-primary/40 bg-primary/10 text-primary"
      : link === "awaiting_read"
        ? "border-warning/40 bg-warning/10 text-warning"
        : "border-border bg-card/40 text-muted-foreground";

  return (
    <div className={`flex items-start gap-2 rounded-lg border p-3 text-xs ${tone}`}>
      <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span className="space-y-1">
        <strong className="block">{EXECUTOR_LINK_LABEL[link]}</strong>
        <span className="block text-muted-foreground">
          {EXECUTOR_LINK_HINT[link]}
          {link === "not_connected" ? (
            <>
              {" "}
              <Link to="/app/connect" className="text-primary hover:underline">
                Connect Binance
              </Link>
              .
            </>
          ) : null}
        </span>
      </span>
    </div>
  );
}
