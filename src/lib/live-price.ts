// ETH/USDT live ticker. Hits our /api/public/engine/eth-price proxy.
import { useEffect, useState } from "react";

export interface LivePrice {
  symbol: string;
  price: number;
  changePct: number;
  high: number;
  low: number;
  status: "connecting" | "live" | "error";
}

export function useLivePrice(symbol = "ETHUSDT"): LivePrice | null {
  const [state, setState] = useState<LivePrice | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const res = await fetch("/api/public/engine/eth-price");
        if (!res.ok) throw new Error(`http ${res.status}`);
        const j = await res.json();
        if (!cancelled) {
          setState({
            symbol: j.symbol ?? symbol,
            price: j.price,
            changePct: j.changePct,
            high: j.high,
            low: j.low,
            status: "live",
          });
        }
      } catch {
        if (!cancelled) {
          setState((s) => s ? { ...s, status: "error" } : { symbol, price: 0, changePct: 0, high: 0, low: 0, status: "error" });
        }
      }
    }
    tick();
    const id = setInterval(tick, 10_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [symbol]);

  return state;
}
