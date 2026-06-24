// Public ETH/USDT 24h ticker proxy. Avoids browser CORS issues.
import { createFileRoute } from "@tanstack/react-router";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

export const Route = createFileRoute("/api/public/engine/eth-price")({
  server: {
    handlers: {
      OPTIONS: async () => new Response(null, { status: 204, headers: CORS }),
      GET: async () => {
        try {
          const res = await fetch("https://api.binance.com/api/v3/ticker/24hr?symbol=ETHUSDT", {
            headers: { Accept: "application/json" },
          });
          if (!res.ok) throw new Error(`upstream ${res.status}`);
          const j = await res.json() as Record<string, string>;
          const body = {
            symbol: j.symbol,
            price: Number(j.lastPrice),
            changePct: Number(j.priceChangePercent),
            high: Number(j.highPrice),
            low: Number(j.lowPrice),
            volume: Number(j.volume),
            quoteVolume: Number(j.quoteVolume),
            updatedAt: new Date().toISOString(),
          };
          return new Response(JSON.stringify(body), {
            status: 200,
            headers: { "Content-Type": "application/json", "Cache-Control": "public, max-age=5", ...CORS },
          });
        } catch (e) {
          return new Response(JSON.stringify({ error: String(e) }), {
            status: 502,
            headers: { "Content-Type": "application/json", ...CORS },
          });
        }
      },
    },
  },
});
