import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

type TickerData = {
  lastPrice: string;
  priceChangePercent: string;
  prevPrice?: string;
};

const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"] as const;
type Symbol = (typeof SYMBOLS)[number];

const fmtPrice = (v: string) => {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString(undefined, {
    minimumFractionDigits: n >= 100 ? 2 : 4,
    maximumFractionDigits: n >= 100 ? 2 : 4,
  });
};

export function BinanceTicker() {
  const [data, setData] = useState<Record<string, TickerData>>({});
  const [status, setStatus] = useState<"connecting" | "live" | "error">("connecting");
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const flashRef = useRef<Record<string, "up" | "down" | null>>({});
  const [, force] = useState(0);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let cancelled = false;
    let retry: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      const streams = SYMBOLS.map((s) => `${s.toLowerCase()}@ticker`).join("/");
      ws = new WebSocket(`wss://stream.binance.com:9443/stream?streams=${streams}`);

      ws.onopen = () => setStatus("live");
      ws.onerror = () => setStatus("error");
      ws.onclose = () => {
        if (cancelled) return;
        setStatus("connecting");
        retry = setTimeout(connect, 2000);
      };
      ws.onmessage = (evt) => {
        try {
          const msg = JSON.parse(evt.data);
          const d = msg.data;
          if (!d?.s) return;
          setData((prev) => {
            const prior = prev[d.s];
            const priorPrice = prior?.lastPrice;
            if (priorPrice && priorPrice !== d.c) {
              flashRef.current[d.s] = Number(d.c) > Number(priorPrice) ? "up" : "down";
              setTimeout(() => {
                flashRef.current[d.s] = null;
                force((n) => n + 1);
              }, 400);
            }
            return {
              ...prev,
              [d.s]: { lastPrice: d.c, priceChangePercent: d.P, prevPrice: priorPrice },
            };
          });
          setUpdatedAt(new Date());
        } catch {
          /* ignore */
        }
      };
    };

    connect();
    return () => {
      cancelled = true;
      if (retry) clearTimeout(retry);
      ws?.close();
    };
  }, []);

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="relative flex h-2 w-2">
            {status === "live" && (
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary opacity-75" />
            )}
            <span
              className={cn(
                "relative inline-flex h-2 w-2 rounded-full",
                status === "live" && "bg-primary",
                status === "connecting" && "bg-yellow-500",
                status === "error" && "bg-[var(--destructive)]",
              )}
            />
          </span>
          <h3 className="text-sm font-medium">
            {status === "live" ? "Live" : status === "connecting" ? "Connecting" : "Disconnected"} · Binance WS
          </h3>
        </div>
        <p className="text-xs text-muted-foreground">
          {updatedAt ? `Updated ${updatedAt.toLocaleTimeString()}` : "Waiting…"}
        </p>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {SYMBOLS.map((sym: Symbol) => {
          const t = data[sym];
          const pct = t ? Number(t.priceChangePercent) : 0;
          const up = pct >= 0;
          const flash = flashRef.current[sym];
          return (
            <div
              key={sym}
              className={cn(
                "rounded-md border border-border/60 bg-background/40 p-3 transition-colors duration-300",
                flash === "up" && "border-primary/60 bg-primary/10",
                flash === "down" && "border-[var(--destructive)]/60 bg-[var(--destructive)]/10",
              )}
            >
              <div className="text-xs text-muted-foreground">
                {sym.replace("USDT", "/USDT")}
              </div>
              <div className="mt-1 font-mono text-base font-semibold tabular-nums">
                {t ? fmtPrice(t.lastPrice) : "—"}
              </div>
              <div
                className={cn(
                  "mt-0.5 text-xs font-medium tabular-nums",
                  up ? "text-primary" : "text-[var(--destructive)]",
                )}
              >
                {t ? `${up ? "+" : ""}${pct.toFixed(2)}%` : "—"}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
