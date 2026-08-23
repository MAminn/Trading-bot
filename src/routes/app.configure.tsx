import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { Sliders, ExternalLink, Loader2, Check, AlertTriangle, Radio, Info } from "lucide-react";
import { toast } from "sonner";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { useEngineConfig, useUpdateConfig, fmtUSD, type EngineConfigRow } from "@/lib/engine";
import { useExecutorStatus } from "@/lib/executor";
import { keysConnected, useBinanceKeyInfo } from "@/lib/binance-keys";
import {
  EXECUTOR_CONNECTED_TITLE,
  EXECUTOR_LIVE_ORDER_REQUIREMENTS,
  EXECUTOR_READS_SETTINGS,
  resolveExecutorLink,
} from "@/lib/executor-link";
import { ExecutorLinkRow } from "@/components/ExecutorLinkRow";
import {
  LIVE_ORDER_CAP_MAX_USD,
  LIVE_ORDER_CAP_MIN_TRADE_USD,
  REQUESTED_EXECUTION_MODES,
  formatViolations,
  isRequestedLiveMode,
  validateLiveState,
  type RequestedExecutionMode,
} from "@/lib/live-controls";
import {
  ALLOC_MAX,
  ALLOC_MIN,
  DEFAULT_SIZING_MODE,
  LEVERAGE_BY_ALLOC,
  LEVERAGE_MAX,
  LEVERAGE_MIN,
  capitalFromAllocation,
  isSizingMode,
  liquidationPct,
  type SizingMode,
} from "@/lib/sizing";

export const Route = createFileRoute("/app/configure")({
  head: () => ({ meta: [{ title: "Configure — Helix" }] }),
  component: Configure,
});

// Allocation mode: 10 discrete states, nothing between adjacent steps.
const allocToIndex = (a: number) => ALLOC_MAX - a;
const indexToAlloc = (i: number) => ALLOC_MAX - i;
const clampAlloc = (a: number) =>
  Math.min(ALLOC_MAX, Math.max(ALLOC_MIN, Math.round(a)));
const clampLeverage = (l: number) =>
  Math.min(LEVERAGE_MAX, Math.max(LEVERAGE_MIN, Math.round(l)));
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
  const [sizingMode, setSizingMode] = useState<SizingMode>(DEFAULT_SIZING_MODE);
  const [accountSize, setAccountSize] = useState("");
  const [allocPct, setAllocPct] = useState<number>(ALLOC_MAX);
  // Free leverage, used in full_capital only — kept separate so switching
  // modes never writes an allocation-derived value into a free field.
  const [freeLeverage, setFreeLeverage] = useState<number>(LEVERAGE_MIN);

  useEffect(() => {
    if (!config) return;
    // An unknown stored mode shows as Allocation — the narrower of the two.
    setSizingMode(isSizingMode(config.sizing_mode) ? config.sizing_mode : DEFAULT_SIZING_MODE);

    // account_size_usd is now first-class. Fall back to the old lossy
    // reconstruction only for rows written before the migration.
    const stored = Number(config.account_size_usd);
    if (Number.isFinite(stored) && stored > 0) {
      setAccountSize(String(Math.round(stored)));
    } else {
      const pct = Number(config.capital_allocation_pct ?? 100);
      const cap = Number(config.capital_usd ?? 0);
      setAccountSize(String(Math.round(pct > 0 ? cap / (pct / 100) : cap)));
    }

    const rawAlloc = Number(config.capital_allocation_pct);
    if (Number.isFinite(rawAlloc) && rawAlloc >= ALLOC_MIN && rawAlloc <= ALLOC_MAX) {
      setAllocPct(clampAlloc(rawAlloc));
    } else {
      const rawLev = Number(config.leverage);
      setAllocPct(Number.isFinite(rawLev) ? allocFromLeverage(rawLev) : ALLOC_MAX);
    }

    const rawLev = Number(config.leverage);
    setFreeLeverage(Number.isFinite(rawLev) ? clampLeverage(rawLev) : LEVERAGE_MIN);
  }, [config]);

  const isFullCapital = sizingMode === "full_capital";
  const accountSizeNum = Number(accountSize) || 0;
  const leverage = isFullCapital ? freeLeverage : (LEVERAGE_BY_ALLOC[allocPct] ?? 1);
  const capitalUsd = useMemo(
    () => (isFullCapital ? accountSizeNum : capitalFromAllocation(accountSizeNum, allocPct)),
    [isFullCapital, accountSizeNum, allocPct],
  );

  async function onSave(e: React.FormEvent) {
    e.preventDefault();
    if (!(accountSizeNum > 0)) {
      toast.error("Account size must be greater than 0");
      return;
    }
    if (capitalUsd < 1) {
      toast.error("Account size is too small for the selected allocation");
      return;
    }
    try {
      // Every sizing patch declares its mode. full_capital deliberately omits
      // capital_allocation_pct — it is meaningless there and the server rejects it.
      await update.mutateAsync(
        isFullCapital
          ? {
              sizing_mode: "full_capital",
              account_size_usd: accountSizeNum,
              capital_usd: accountSizeNum,
              leverage,
            }
          : {
              sizing_mode: "allocation",
              account_size_usd: accountSizeNum,
              capital_usd: capitalUsd,
              capital_allocation_pct: allocPct,
              leverage,
            },
      );
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
            <div>
              <span className="mb-1 block text-xs font-medium uppercase tracking-wider text-muted-foreground">
                Sizing mode
              </span>
              <div className="inline-flex rounded-lg border border-border p-1">
                <ModeButton
                  active={!isFullCapital}
                  label="Allocation"
                  onClick={() => setSizingMode("allocation")}
                />
                <ModeButton
                  active={isFullCapital}
                  label="Full capital"
                  onClick={() => setSizingMode("full_capital")}
                />
              </div>
              <p className="mt-2 text-xs text-muted-foreground">
                {isFullCapital
                  ? "Positions are sized off your full account size, bounded only by available margin and exchange limits."
                  : "Positions are sized off a fixed percentage of your account, with internal notional caps applied."}
              </p>
            </div>

            {/* A sizing input, not a balance. Nothing here moves money or
                reflects a wallet: it is the number the strategy sizes against,
                and it is capped independently by the live order cap. */}
            <Field label="Account size (USD)" value={accountSize} onChange={setAccountSize} type="number" />
            <p className="-mt-2 text-xs text-muted-foreground">
              Used to size orders. This is not your Binance balance — your real wallet
              balance is read from Binance and shown on the Dashboard and Engine pages.
            </p>

            <div className="grid gap-6 md:grid-cols-2">
              <div>
                <div className="flex items-baseline justify-between">
                  <span className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Leverage</span>
                  <span className="font-mono text-lg font-semibold text-primary">{leverage}×</span>
                </div>
                {isFullCapital ? (
                  <>
                    <Slider
                      className="mt-3"
                      min={LEVERAGE_MIN} max={LEVERAGE_MAX} step={1}
                      value={[freeLeverage]}
                      onValueChange={(v) => setFreeLeverage(clampLeverage(v[0]))}
                    />
                    <div className="mt-2 flex justify-between font-mono text-[10px] text-muted-foreground">
                      <span>{LEVERAGE_MIN}×</span><span>{LEVERAGE_MAX}×</span>
                    </div>
                  </>
                ) : (
                  <>
                    <Slider
                      className="mt-3"
                      min={0} max={9} step={1}
                      value={[allocToIndex(allocPct)]}
                      onValueChange={(v) => setAllocPct(indexToAlloc(v[0]))}
                    />
                    <div className="mt-2 flex justify-between font-mono text-[10px] text-muted-foreground">
                      <span>1×</span><span>90×</span>
                    </div>
                  </>
                )}
              </div>

              {/* Allocation is meaningless in full_capital — hidden, not disabled. */}
              {!isFullCapital && (
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
              )}
            </div>

            {isFullCapital ? (
              <>
                <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span className="space-y-1">
                    <strong className="block">Full capital mode — internal notional caps do not apply.</strong>
                    <span className="block text-destructive/90">
                      Order size is bounded only by your available margin and the
                      exchange's leverage bracket limits. At {leverage}× a roughly{" "}
                      <span className="font-mono font-semibold">
                        {liquidationPct(leverage).toFixed(2)}%
                      </span>{" "}
                      adverse move liquidates the position.
                    </span>
                  </span>
                </div>

                <div className="grid gap-3 rounded-lg border border-border bg-card/40 p-4 text-xs md:grid-cols-3">
                  <KV k="Account size" v={fmtUSD(accountSizeNum)} />
                  <KV k="Leverage" v={`${leverage}×`} />
                  <KV k="Target notional" v={fmtUSD(accountSizeNum * leverage)} />
                </div>
              </>
            ) : (
              <>
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
              </>
            )}

            <div>
              {/* Read-only mirror of the saved mode. The control itself lives in
                  Live execution below — auto-execute is a real, enforced setting
                  the executor reads, so showing it here as "coming soon" beside a
                  working toggle would misdescribe the product in both directions. */}
              <span className="mb-1 block text-xs font-medium uppercase tracking-wider text-muted-foreground">Mode</span>
              <div className="flex gap-2">
                <ModeChip label="Signal only" active={config?.mode !== "auto"} />
                <ModeChip label="Auto-execute" active={config?.mode === "auto"} />
              </div>
              <p className="mt-2 text-xs text-muted-foreground">
                Set this in Live execution below. Auto-execute is one of several
                gates: on its own it does not place an order.
              </p>
            </div>
          </>
        )}

        <div className="flex items-center justify-end gap-3">
          <span className="text-xs text-muted-foreground">
            Current: {config ? `${fmtUSD(Number(config.capital_usd))} · ${config.leverage}× · ${config.sizing_mode === "full_capital" ? "full capital" : `${config.capital_allocation_pct ?? 100}% alloc`}` : "—"}
          </span>
          <button type="submit" disabled={update.isPending || isLoading}
            className="inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary to-accent px-4 py-2 text-sm font-semibold text-primary-foreground disabled:opacity-60">
            {update.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />} Save
          </button>
        </div>
      </form>

      <LiveExecutionSection config={config} />
    </div>
  );
}

const MODE_COPY: Record<RequestedExecutionMode, { label: string; hint: string }> = {
  OFF: {
    label: "Off",
    hint: "No exchange connectivity requested. Signals are recorded only.",
  },
  LIVE_READ: {
    label: "Live · read-only",
    hint: "Read mainnet balances and positions. Structurally cannot place an order.",
  },
  LIVE_TRADE: {
    label: "Live trading",
    hint: "Place real orders on Binance mainnet with real funds.",
  },
};

function LiveExecutionSection({ config }: { config: EngineConfigRow | null | undefined }) {
  const update = useUpdateConfig();
  // Two independent sources, on purpose. The key metadata answers "has this
  // client connected a wallet"; the executor telemetry answers "has the
  // executor actually used those keys". Neither can stand in for the other.
  const executor = useExecutorStatus();
  const keyInfo = useBinanceKeyInfo();
  const link = resolveExecutorLink(executor.data, {
    keysConnected: keysConnected(keyInfo),
  });
  // Neither query has answered yet. Showing the not-connected wording to a
  // client who connected months ago, for the half-second before the key
  // metadata lands, is exactly the kind of false statement this page is being
  // fixed for.
  const linkPending = keyInfo.isLoading || executor.isLoading;
  // Fail-closed defaults, matching the database: a row that has never been
  // configured displays as OFF / $0 / auto disabled / no full-capital consent.
  const [execMode, setExecMode] = useState<RequestedExecutionMode>("OFF");
  const [cap, setCap] = useState("0");
  const [allowFullCapital, setAllowFullCapital] = useState(false);
  const [autoExecute, setAutoExecute] = useState(false);

  useEffect(() => {
    if (!config) return;
    const stored = config.execution_mode;
    setExecMode(
      (REQUESTED_EXECUTION_MODES as readonly string[]).includes(stored)
        ? (stored as RequestedExecutionMode)
        : "OFF",
    );
    const storedCap = Number(config.live_order_cap_usd);
    setCap(String(Number.isFinite(storedCap) && storedCap > 0 ? storedCap : 0));
    setAllowFullCapital(!!config.live_allow_full_capital);
    setAutoExecute(config.mode === "auto");
  }, [config]);

  const capNum = Number(cap) || 0;
  const isLive = isRequestedLiveMode(execMode);

  // The same rule module the server and the database enforce, run against the
  // state this form would produce — so the UI refuses what the server would.
  const violations = useMemo(
    () =>
      validateLiveState({
        execution_mode: execMode,
        live_order_cap_usd: capNum,
        live_allow_full_capital: allowFullCapital,
        sizing_mode: config?.sizing_mode,
        demo_mode: config?.demo_mode,
      }),
    [execMode, capNum, allowFullCapital, config?.sizing_mode, config?.demo_mode],
  );

  async function onSave(e: React.FormEvent) {
    e.preventDefault();
    if (violations.length > 0) {
      toast.error(formatViolations(violations));
      return;
    }
    try {
      await update.mutateAsync({
        // The control tuple is always sent as a set: the server rejects a
        // partial live-execution patch, because validating one field without
        // the others it pairs with is not validation.
        execution_mode: execMode,
        live_order_cap_usd: capNum,
        live_allow_full_capital: allowFullCapital,
        mode: autoExecute ? "auto" : "signal_only",
      });
      toast.success("Live execution settings saved");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Save failed");
    }
  }

  const dirty =
    !!config &&
    (execMode !== config.execution_mode ||
      capNum !== Number(config.live_order_cap_usd) ||
      allowFullCapital !== !!config.live_allow_full_capital ||
      autoExecute !== (config.mode === "auto"));

  return (
    <form onSubmit={onSave} className="card-elevated space-y-6 p-6">
      <div className="flex items-center gap-2 text-sm font-medium">
        <Radio className="h-4 w-4 text-primary" /> Live execution
      </div>

      {/* The single most important thing on this page: what saving here does.
          Since multi-tenant onboarding the executor polls the roster and reads
          each user's config every cycle, so these settings ARE live inputs —
          but reading a setting is not the same as being able to act on it, and
          the gate list is what separates the two. */}
      <div className="flex items-start gap-2 rounded-lg border border-primary/40 bg-primary/10 p-3 text-xs">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
        <span className="space-y-1">
          <strong className="block text-primary">{EXECUTOR_CONNECTED_TITLE}</strong>
          <span className="block text-muted-foreground">
            {EXECUTOR_READS_SETTINGS} {EXECUTOR_LIVE_ORDER_REQUIREMENTS} What the
            executor is <em>actually</em> doing is shown on the{" "}
            <Link to="/app/engine" className="text-primary hover:underline">
              Engine page
            </Link>
            .
          </span>
        </span>
      </div>

      {/* This client's own exchange link, in the same words every other page
          uses. No figure is named here — only whether a signed read of their
          Binance account has happened at all. */}
      <ExecutorLinkRow link={link} pending={linkPending} />

      <div>
        <span className="mb-1 block text-xs font-medium uppercase tracking-wider text-muted-foreground">
          Requested execution mode
        </span>
        <div className="inline-flex flex-wrap rounded-lg border border-border p-1">
          {REQUESTED_EXECUTION_MODES.map((m) => (
            <ModeButton
              key={m}
              active={execMode === m}
              label={MODE_COPY[m].label}
              onClick={() => setExecMode(m)}
            />
          ))}
        </div>
        <p className="mt-2 text-xs text-muted-foreground">{MODE_COPY[execMode].hint}</p>
      </div>

      <label className="block">
        <span className="mb-1 block text-xs font-medium uppercase tracking-wider text-muted-foreground">
          Live order cap (USD)
        </span>
        <input
          type="number"
          value={cap}
          min={0}
          max={LIVE_ORDER_CAP_MAX_USD}
          onChange={(e) => setCap(e.target.value)}
          className="w-full rounded-lg border border-border bg-input/40 px-3 py-2.5 font-mono text-sm focus:border-primary/60 focus:outline-none focus:ring-2 focus:ring-primary/20"
        />
        <span className="mt-1 block text-xs text-muted-foreground">
          Absolute ceiling per order, applied on top of every sizing rule.
          Maximum ${LIVE_ORDER_CAP_MAX_USD}; live trading requires at least $
          {LIVE_ORDER_CAP_MIN_TRADE_USD}, since smaller orders fall under the
          exchange minimum notional.
        </span>
      </label>

      <ToggleRow
        label="Auto-execute"
        checked={autoExecute}
        onChange={setAutoExecute}
        disabled={update.isPending}
        hint={
          autoExecute
            ? "Accepted signals are intended to be executed, not just recorded."
            : "Signals are recorded only. This is the safe default."
        }
      />

      <ToggleRow
        label="Allow full-capital sizing on live"
        checked={allowFullCapital}
        onChange={setAllowFullCapital}
        disabled={update.isPending}
        tone={allowFullCapital ? "danger" : undefined}
        hint="Full-capital sizing has no internal notional ceiling. Required before live trading may use it — the executor additionally requires its own environment flag."
      />

      {isLive && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            {execMode === "LIVE_TRADE"
              ? `Requesting real orders on Binance mainnet, capped at ${fmtUSD(capNum)} per order.`
              : "Requesting mainnet access. Read-only: no order can be placed in this mode."}
          </span>
        </div>
      )}

      {violations.length > 0 && (
        <ul className="space-y-1 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
          {violations.map((v) => (
            <li key={v.field}>{v.message}</li>
          ))}
        </ul>
      )}

      <div className="flex items-center justify-end gap-3">
        <span className="text-xs text-muted-foreground">
          Saved:{" "}
          {config
            ? `${config.execution_mode} · ${fmtUSD(Number(config.live_order_cap_usd))} cap · auto ${
                config.mode === "auto" ? "on" : "off"
              }`
            : "—"}
        </span>
        <button
          type="submit"
          disabled={update.isPending || violations.length > 0 || !dirty}
          className="inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary to-accent px-4 py-2 text-sm font-semibold text-primary-foreground disabled:opacity-60"
        >
          {update.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Check className="h-4 w-4" />
          )}{" "}
          Save live settings
        </button>
      </div>
    </form>
  );
}

function ToggleRow({
  label,
  hint,
  checked,
  onChange,
  disabled,
  tone,
}: {
  label: string;
  hint: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  tone?: "danger";
}) {
  return (
    <div
      className={`flex items-start justify-between gap-4 rounded-lg border p-4 ${
        tone === "danger" ? "border-destructive/40 bg-destructive/5" : "border-border bg-card/40"
      }`}
    >
      <div className="min-w-0">
        <div className="text-sm font-medium">{label}</div>
        <p className="mt-1 text-xs text-muted-foreground">{hint}</p>
      </div>
      <Switch checked={checked} onCheckedChange={onChange} disabled={disabled} aria-label={label} />
    </div>
  );
}

/** A read-only chip showing which mode is saved. Not a button: the control is
 *  the auto-execute toggle in Live execution, and offering two of them would be
 *  two places to disagree. */
function ModeChip({ label, active }: { label: string; active: boolean }) {
  return (
    <span
      className={`rounded-lg border px-3 py-2 text-sm font-medium ${
        active
          ? "border-primary/60 bg-primary/15 text-primary"
          : "border-border bg-card/40 text-muted-foreground"
      }`}
    >
      {label}
      {active ? " (active)" : ""}
    </span>
  );
}

function ModeButton({ active, label, onClick }: { active: boolean; label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-md px-4 py-2 text-sm font-medium transition-colors ${
        active
          ? "bg-primary/15 text-primary ring-1 ring-primary/60"
          : "text-muted-foreground hover:text-foreground"
      }`}
    >
      {label}
    </button>
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
