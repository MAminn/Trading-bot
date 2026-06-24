import * as Popover from "@radix-ui/react-popover";
import { Bell, Check, CircleDot, Info, ShieldAlert, Trash2 } from "lucide-react";
import { formatRelative, useAlerts, type AlertItem } from "@/lib/alerts";

export function NotificationsBell() {
  const { alerts, unreadCount, markAllRead, clear, browserPermission, requestBrowserPermission } = useAlerts();

  return (
    <Popover.Root onOpenChange={(open) => { if (open) markAllRead(); }}>
      <Popover.Trigger asChild>
        <button
          className="relative rounded-lg border border-border bg-card/50 p-2 text-muted-foreground hover:text-foreground"
          aria-label="Notifications"
        >
          <Bell className="h-4 w-4" />
          {unreadCount > 0 && (
            <span className="absolute -right-1 -top-1 grid h-4 min-w-4 place-items-center rounded-full bg-primary px-1 font-mono text-[10px] font-semibold text-primary-foreground shadow-[0_0_8px_oklch(0.85_0.18_165)]">
              {unreadCount > 9 ? "9+" : unreadCount}
            </span>
          )}
        </button>
      </Popover.Trigger>

      <Popover.Portal>
        <Popover.Content
          align="end"
          sideOffset={10}
          className="z-50 w-[380px] origin-top-right rounded-xl border border-border bg-popover/95 shadow-[0_30px_60px_-20px_oklch(0_0_0/70%)] backdrop-blur-xl data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95"
        >
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <div>
              <div className="font-display text-sm font-semibold">Alerts</div>
              <div className="text-[11px] text-muted-foreground">
                {alerts.length === 0 ? "Quiet for now" : `${alerts.length} event${alerts.length === 1 ? "" : "s"} · live feed`}
              </div>
            </div>
            <button
              onClick={clear}
              className="rounded-md p-1.5 text-muted-foreground hover:bg-secondary hover:text-foreground"
              aria-label="Clear all"
              disabled={alerts.length === 0}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>

          {browserPermission === "default" && (
            <div className="border-b border-border bg-primary/5 px-4 py-3">
              <div className="text-xs font-medium">Enable system notifications</div>
              <p className="mt-0.5 text-[11px] text-muted-foreground">
                Get an OS-level alert on every BUY signal and risk breach, even when the tab is in the background.
              </p>
              <button
                onClick={requestBrowserPermission}
                className="mt-2 inline-flex items-center gap-1.5 rounded-md bg-gradient-to-r from-primary to-accent px-2.5 py-1 text-[11px] font-semibold text-primary-foreground"
              >
                <Check className="h-3 w-3" /> Allow
              </button>
            </div>
          )}

          <div className="max-h-[420px] overflow-y-auto">
            {alerts.length === 0 ? (
              <div className="px-4 py-10 text-center text-xs text-muted-foreground">
                You'll see BUY signals and risk breaches here.
              </div>
            ) : (
              <ul className="divide-y divide-border">
                {alerts.map((a) => (
                  <li key={a.id}>
                    <AlertRow alert={a} />
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function AlertRow({ alert }: { alert: AlertItem }) {
  const styles = kindStyles(alert.kind);
  return (
    <div className="flex gap-3 px-4 py-3 hover:bg-secondary/40">
      <div className={`mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-lg ${styles.bg}`}>
        <styles.Icon className={`h-4 w-4 ${styles.fg}`} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <div className="truncate text-sm font-medium">{alert.title}</div>
          <div className="shrink-0 font-mono text-[10px] text-muted-foreground">{formatRelative(alert.createdAt)}</div>
        </div>
        <div className="mt-0.5 text-xs text-muted-foreground">{alert.message}</div>
        {alert.meta && <div className="mt-1 font-mono text-[10px] text-muted-foreground/70">{alert.meta}</div>}
      </div>
    </div>
  );
}

function kindStyles(kind: AlertItem["kind"]) {
  switch (kind) {
    case "buy_signal":
      return { Icon: CircleDot, bg: "bg-success/15", fg: "text-success" };
    case "sell_signal":
      return { Icon: CircleDot, bg: "bg-destructive/15", fg: "text-destructive" };
    case "risk_breach":
      return { Icon: ShieldAlert, bg: "bg-warning/15", fg: "text-warning" };
    default:
      return { Icon: Info, bg: "bg-accent/15", fg: "text-accent" };
  }
}
