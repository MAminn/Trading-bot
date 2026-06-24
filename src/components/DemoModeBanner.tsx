import { AlertTriangle } from "lucide-react";
import { useEngineConfig, useEngineStatus, heartbeatFresh } from "@/lib/engine";

/**
 * Persistent, hard-to-miss banner shown on every page whenever the user has
 * Demo / Test mode enabled. The dashboard data underneath is simulated and
 * MUST NOT be confused with real trading signals.
 */
export function DemoModeBanner() {
  const { data: config } = useEngineConfig();
  const { data: status } = useEngineStatus();

  if (!config?.demo_mode) return null;

  // Detect a real engine heartbeat (message without [DEMO]) within last 30 min
  const msg = status?.message ?? "";
  const isRealHeartbeat =
    !!status?.last_heartbeat &&
    heartbeatFresh(status.last_heartbeat) &&
    msg.length > 0 &&
    !msg.includes("[DEMO]");

  return (
    <div className="sticky top-0 z-50 border-b border-warning/40 bg-warning/15 text-warning-foreground">
      <div className="mx-auto flex max-w-7xl items-center gap-3 px-4 py-2 text-sm">
        <AlertTriangle className="h-4 w-4 shrink-0 text-warning" />
        <div className="min-w-0 flex-1">
          <span className="font-semibold text-warning">DEMO MODE</span>{" "}
          <span className="text-foreground/90">
            simulated data for testing the dashboard. These are <strong>NOT real
            trading signals</strong>. Turn off before connecting Binance or the
            live engine.
          </span>
          {isRealHeartbeat && (
            <div className="mt-1 text-xs font-medium text-destructive">
              Real engine detected — turn off Demo mode to avoid mixed data.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
