// V22 LONG/SHORT engine constants — exported from
// Archive1/v22_live_decision_engine_config.json + v22_live_long_candidate_engine.json
// These are FROZEN training parameters. Do not tweak without retraining.
export const V22 = {
  symbol: "ETHUSDT",
  tf: "15m",
  // Causal feature quantiles (train-only)
  q: {
    range70: 0.005515807968854227,
    range40: 0.003414193826232752,
    ret4_65: 0.0017472798929056839,
    ret12_40: -0.001759789279492361,
    ret24_25: -0.007987277324288791,
    closepos60: 0.6216255264628551,
    closepos75: 0.7744168440006667,
    lwick60: 0.30851063829790365,
    bbw30: 0.011527622652476305,
  },
  // V22 management
  mgmt: {
    decision_bar: 4,
    provisional_sl_atr_default: 2.1,
    provisional_sl_atr_weak_regime: 2.45,
    normal_sl_atr: 1.35,
    trail_start_atr: 0.65,
    trail_dist_atr: 0.35,
    tp_atr: 1.7,
    max_hold_bars: 24, // 24 × 15m = 6h
  },
  // ML gates from locked_final_target
  ml: { long_threshold: 0.40, short_threshold: 0.39 },
  // Entry policy
  entry: { same_bar_priority: "LONG_FIRST" as const, no_flip: true },
} as const;

export type Kline = {
  openTime: number; open: number; high: number; low: number; close: number; volume: number;
};

export async function fetchKlines(limit = 50): Promise<Kline[]> {
  const url = `https://api.binance.com/api/v3/klines?symbol=${V22.symbol}&interval=${V22.tf}&limit=${limit}`;
  const r = await fetch(url, { headers: { accept: "application/json" } });
  if (!r.ok) throw new Error(`binance klines ${r.status}`);
  const j = (await r.json()) as unknown[][];
  return j.map((k) => ({
    openTime: Number(k[0]),
    open: Number(k[1]), high: Number(k[2]), low: Number(k[3]), close: Number(k[4]),
    volume: Number(k[5]),
  }));
}

// Wilder ATR (period 14) — returns ATR in price units.
export function atr14(ks: Kline[]): number {
  const n = 14;
  if (ks.length < n + 1) return 0;
  let trSum = 0;
  for (let i = ks.length - n; i < ks.length; i++) {
    const h = ks[i].high, l = ks[i].low, pc = ks[i - 1].close;
    trSum += Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc));
  }
  return trSum / n;
}

// Features computed at the close of the latest CLOSED 15m bar (ks[last]).
export function features(ks: Kline[]) {
  const last = ks.length - 1;
  const k = ks[last];
  const range = (k.high - k.low) / k.close;
  const ret = (n: number) => last - n >= 0 ? (k.close - ks[last - n].close) / ks[last - n].close : 0;
  const body = k.high - k.low || 1e-9;
  const closepos = (k.close - k.low) / body;
  const lwick = (Math.min(k.open, k.close) - k.low) / body;
  return { range, ret4: ret(4), ret12: ret(12), ret24: ret(24), closepos, lwick, atr: atr14(ks), close: k.close, barTime: k.openTime };
}

// Rule side at the bar close. Returns -1/0/1 with a human-readable reason.
export function ruleSide(f: ReturnType<typeof features>): { side: -1 | 0 | 1; reason: string } {
  const q = V22.q;
  // LONG: bullish expansion bar — strong range, positive ret4, close in upper body
  const longExpansion = f.range >= q.range40 && f.ret4 >= q.ret4_65 && f.closepos >= q.closepos60;
  // SHORT: bearish expansion — strong range, negative ret4, close in lower body
  const shortExpansion = f.range >= q.range40 && f.ret4 <= -q.ret4_65 && f.closepos <= 1 - q.closepos60;
  // LONG_FIRST priority
  if (longExpansion) return { side: 1, reason: `LONG expansion: range=${(f.range * 100).toFixed(2)}% ret4=${(f.ret4 * 100).toFixed(2)}% closepos=${f.closepos.toFixed(2)}` };
  if (shortExpansion) return { side: -1, reason: `SHORT expansion: range=${(f.range * 100).toFixed(2)}% ret4=${(f.ret4 * 100).toFixed(2)}% closepos=${f.closepos.toFixed(2)}` };
  return { side: 0, reason: `no setup: range=${(f.range * 100).toFixed(2)}% ret4=${(f.ret4 * 100).toFixed(2)}%` };
}

// Composite ML-proxy probability ∈ [0,1]. Without the joblib model we
// approximate using the same normalized features the model is trained on.
// Mean-zero standardisation against the training quantiles, mapped through
// a sigmoid. This is intentionally conservative.
export function mlProxy(f: ReturnType<typeof features>, side: 1 | -1): number {
  const q = V22.q;
  const dir = side; // align features with side
  const z =
    1.2 * dir * (f.ret4 / Math.max(q.ret4_65, 1e-6)) +
    0.8 * (f.range / Math.max(q.range70, 1e-6) - 1) +
    0.6 * dir * (f.closepos - 0.5) * 2 +
    0.3 * dir * (f.ret12 / Math.max(Math.abs(q.ret12_40), 1e-6));
  const p = 1 / (1 + Math.exp(-z));
  return Math.max(0.01, Math.min(0.99, p));
}

// V22 entry/exit price math at fill (next 15m open ≈ current price for live).
export function buildLevels(entry: number, atr: number, side: 1 | -1) {
  const m = V22.mgmt;
  const tp = side === 1 ? entry + m.tp_atr * atr : entry - m.tp_atr * atr;
  const sl = side === 1 ? entry - m.normal_sl_atr * atr : entry + m.normal_sl_atr * atr;
  return { tp, sl };
}

// Trailing stop per V22: once price has moved trail_start_atr in favour,
// stop trails at trail_dist_atr behind the running extreme.
export function trailStop(
  entry: number,
  atr: number,
  side: 1 | -1,
  currentStop: number,
  lastClose: number,
): number {
  const m = V22.mgmt;
  const move = side === 1 ? (lastClose - entry) : (entry - lastClose);
  if (move < m.trail_start_atr * atr) return currentStop;
  const trailed = side === 1
    ? lastClose - m.trail_dist_atr * atr
    : lastClose + m.trail_dist_atr * atr;
  return side === 1 ? Math.max(currentStop, trailed) : Math.min(currentStop, trailed);
}
