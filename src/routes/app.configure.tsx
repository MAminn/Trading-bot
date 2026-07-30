import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { Sliders, ExternalLink, Loader2, Check, AlertTriangle } from "lucide-react";
import { toast } from "sonner";
import { Slider } from "@/components/ui/slider";
import { useEngineConfig, useUpdateConfig, fmtUSD } from "@/lib/engine";

export const Route = createFileRoute("/app/configure")({
  head: () => ({ meta: [{ title: "Configure — Helix" }] }),
  component: Configure,
});

// Allocation is the single source of truth. Leverage is derived, never free.
// 10 discrete states. Nothing exists between adjacent steps.
const ALLOC_MIN = 1;
const ALLOC_MAX = 10;
const LEVERAGE_BY_ALLOC: Record<number, number> = {
  10: 1, 9: 10, 8: 20, 7: 30, 6: 40, 5: 50, 4: 60, 3: 70, 2: 80, 1: 90,
};
const allocToIndex = (a: number) => ALLOC_MAX - a;
const indexToAlloc = (i: number) => ALLOC_MAX - i;
const clampAlloc = (a: number) =>
  Math.min(ALLOC_MAX, Math.max(ALLOC_MIN, Math.round(a)));
const allocFromLeverage = (lv: number) => {
  let best = ALLOC_MAX, bestDiff = Infinity;
  for (const [a, l] of Object.entries(LEVERAGE_BY_ALLOC)) {
    const d = Math.abs(l - lv);
    if (d < bestDiff) { bestDiff = d; best = Number(a); }
  }
  return best;
};

function Configure() {
  const { data: config, isLoading } = useEngineConfig();
  const update = useUpdateConfig();
  const [accountSize, setAccountSize] = useState("");
  const [allocPct, setAllocPct] = useState<number>(ALLOC_MAX);
  const [maxLoss, setMaxLoss] = useState("");
  const [maxPos, setMaxPos] = useState("");

  useEffect(() => {
    if (!config) return;
    const pct = Number(config.capital_allocation_pct ?? 100);
    const cap = Number(config.capital_usd ?? 0);
    const acct = pct > 0 ? cap / (pct / 100) : cap;
    setAccountSize(String(Math.round(acct)));
    const rawAlloc = Number(config.capital_allocation_pct);
    if (Number.isFinite(rawAlloc) && rawAlloc >= ALLOC_MIN && rawAlloc <= ALLOC_MAX) {
      setAllocPct(clampAlloc(rawAlloc));
    } else {
      const rawLev = Number(config.leverage);
      setAllocPct(Number.isFinite(rawLev) ? allocFromLeverage(rawLev) : ALLOC_MAX);
    }
    setMaxLoss(String(config.max_daily_loss_usd));
    setMaxPos(String(config.max_position_size_usd));
  }, [config]);

  const leverage = LEVERAGE_BY_ALLOC[allocPct] ?? 1;
  const capitalUsd = useMemo(
    () => Math.round((Number(accountSize) || 0) * (allocPct / 100)),
    [accountSize, allocPct],
  );

  async function onSave(e: React.FormEvent) {
    e.preventDefault();
    try {
      await update.mutateAsync({
        capital_usd: capitalUsd,
        capital_allocation_pct: allocPct,
        leverage,
        max_daily_loss_usd: Number(maxLoss),
        max_position_size_usd: Number(maxPos),
      });
      toast.success("Configuration saved");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Save failed");
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <div className="flex items-end justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-[0.2em] text-primary">Engine</div>
          <h1 className="mt-2 font-display text-3xl font-semibold">Configure engine</h1>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
            These risk limits and mode are what the external Python engine reads
            on every cycle. Changes apply within seconds.
          </p>
        </div>
        <Link to="/app/engine"
          className="inline-flex items-center gap-2 rounded-lg border border-border bg-card/40 px-3 py-2 text-sm hover:bg-card/70">
          <ExternalLink className="h-4 w-4" /> Engine
        </Link>
      </div>

      <form onSubmit={onSave} className="card-elevated space-y-8 p-6">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Sliders className="h-4 w-4 text-primary" /> Risk + capital
        </div>

        {isLoading ? (
          <div className="text-sm text-muted-foreground">Loading…</div>
        ) : (
          <>
            <Field label="Account size (USD)" value={accountSize} onChange={setAccountSize} type="number" />

            <div className="grid gap-6 md:grid-cols-2">
              <div>
                <div className="flex items-baseline justify-between">
                  <span className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Leverage</span>
                  <span className="font-mono text-lg font-semibold text-primary">{leverage}×</span>
                </div>
                <Slider
                  className="mt-3"
                  min={0} max={9} step={1}
                  value={[allocToIndex(allocPct)]}
                  onValueChange={(v) => setAllocPct(indexToAlloc(v[0]))}
                />
                <div className="mt-2 flex justify-between font-mono text-[10px] text-muted-foreground">
                  <span>1×</span><span>90×</span>
                </div>
              </div>

              <div>
                <div className="flex items-baseline justify-between">
                  <span className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Capital allocation</span>
                  <span className="font-mono text-lg font-semibold text-primary">{allocPct}%</span>
                </div>
                <Slider
                  className="mt-3"
                  min={ALLOC_MIN} max={ALLOC_MAX} step={1}
                  value={[allocPct]}
                  onValueChange={(v) => setAllocPct(clampAlloc(v[0]))}
                />
                <div className="mt-2 flex justify-between font-mono text-[10px] text-muted-foreground">
                  <span>1%</span><span>10%</span>
                </div>
              </div>
            </div>

            <div className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span className="text-warning/90">
                Higher leverage massively increases both gains and losses. 70× means a ~1.4% adverse move can liquidate the position.
              </span>
            </div>

            <p className="text-xs text-muted-foreground">
              Allocation moves in whole percent only; leverage is derived from it.
              Margin committed always equals your allocation — leverage multiplies
              exposure, not capital at risk. The executor clamps leverage to the ETHUSDT
              exchange maximum and caps order notional independently, so live order size
              may be smaller than shown.
            </p>

            <div className="grid gap-3 rounded-lg border border-border bg-card/40 p-4 text-xs md:grid-cols-4">
              <KV k="Strategy capital" v={fmtUSD(capitalUsd)} />
              <KV k="Leverage" v={`${leverage}×`} />
              <KV k="Allocation" v={`${allocPct}%`} />
              <KV k="Notional" v={fmtUSD(capitalUsd * leverage)} />
            </div>

            <div className="space-y-3">
              <div className="grid gap-5 md:grid-cols-2">
                <Field label="Take profit (%)" value={maxLoss} onChange={setMaxLoss} type="number" />
                <Field label="Stop loss (%)" value={maxPos} onChange={setMaxPos} type="number" />
              </div>
              <p className="text-xs text-muted-foreground">
                For these to work effectively use a positive risk/reward ratio — TP should be at least 2× SL.
                Suggested defaults: <span className="font-mono text-foreground">TP 3%</span>, <span className="font-mono text-foreground">SL 1.5%</span>.
                Keep SL below 2% to survive normal volatility; never exceed your account's daily loss tolerance.
              </p>
            </div>

            <div>
              <span className="mb-1 block text-xs font-medium uppercase tracking-wider text-muted-foreground">Mode</span>
              <div className="flex gap-2">
                <button type="button" disabled
                  className="rounded-lg border border-primary/60 bg-primary/15 px-3 py-2 text-sm font-medium text-primary">
                  Signal only (active)
                </button>
                <button type="button" disabled
                  className="cursor-not-allowed rounded-lg border border-border bg-card/40 px-3 py-2 text-sm text-muted-foreground line-through opacity-60">
                  Auto-execute (coming soon)
                </button>
              </div>
              <p className="mt-2 text-xs text-muted-foreground">
                Auto-execution is locked at the database level until Phase 2.
              </p>
            </div>
          </>
        )}

        <div className="flex items-center justify-end gap-3">
          <span className="text-xs text-muted-foreground">
            Current: {config ? `${fmtUSD(Number(config.capital_usd))} · ${config.leverage}× · ${config.capital_allocation_pct ?? 100}% alloc` : "—"}
          </span>
          <button type="submit" disabled={update.isPending || isLoading}
            className="inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary to-accent px-4 py-2 text-sm font-semibold text-primary-foreground disabled:opacity-60">
            {update.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />} Save
          </button>
        </div>
      </form>
    </div>
  );
}

function Field({ label, value, onChange, type = "text" }: { label: string; value: string; onChange: (v: string) => void; type?: string }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium uppercase tracking-wider text-muted-foreground">{label}</span>
      <input
        type={type} value={value} onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-lg border border-border bg-input/40 px-3 py-2.5 font-mono text-sm focus:border-primary/60 focus:outline-none focus:ring-2 focus:ring-primary/20"
      />
    </label>
  );
}

function KV({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="uppercase tracking-wider text-muted-foreground">{k}</span>
      <span className="font-mono font-semibold">{v}</span>
    </div>
  );
}
