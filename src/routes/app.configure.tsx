import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import {
  Sliders,
  ExternalLink,
  Loader2,
  Check,
  AlertTriangle,
  Radio,
  Info,
  Wallet,
} from "lucide-react";
import { toast } from "sonner";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { useEngineConfig, useUpdateConfig, fmtUSD, type EngineConfigRow } from "@/lib/engine";
import { useExecutorStatus } from "@/lib/executor";
import { keysConnected, useBinanceKeyInfo } from "@/lib/binance-keys";
import { resolveWalletDisplay } from "@/lib/wallet-display";
import {
  EXECUTOR_CONNECTED_TITLE,
  EXECUTOR_LIVE_ORDER_REQUIREMENTS,
  EXECUTOR_READS_SETTINGS,
  resolveExecutorLink,
} from "@/lib/executor-link";
import { ExecutorLinkRow } from "@/components/ExecutorLinkRow";
import {
  REQUESTED_EXECUTION_MODES,
  formatViolations,
  isRequestedLiveMode,
  validateLiveState,
  type RequestedExecutionMode,
} from "@/lib/live-controls";
import {
  ALLOCATION_PCTS,
  LEVERAGE_STEPS,
  allocatedMargin,
  liquidationPct,
  targetNotional,
  toAllocationPct,
  toLeverageStep,
  type AllocationPct,
  type LeverageStep,
} from "@/lib/sizing";

export const Route = createFileRoute("/app/configure")({
  head: () => ({ meta: [{ title: "Configure — Helix" }] }),
  component: Configure,
});

function Configure() {
  const { data: config, isLoading } = useEngineConfig();
  const update = useUpdateConfig();
  // The wallet the engine actually sizes against: this user's own Binance
  // USD-M totalWalletBalance, as last read by the executor with this user's own
  // credentials. Read-only here by design — there is no field that can set it,
  // and no figure typed on this page can become a sizing input.
  const executor = useExecutorStatus();
  const keyInfo = useBinanceKeyInfo();
  const wallet = resolveWalletDisplay(executor.data, { keysConnected: keysConnected(keyInfo) });
  const walletPending = keyInfo.isLoading || executor.isLoading;
  const walletUsd = wallet.state === "connected" ? wallet.walletUsd : null;

  // Two independent controls, two independent pieces of state. Nothing derives
  // one from the other, in either direction.
  const [allocPct, setAllocPct] = useState<AllocationPct>(ALLOCATION_PCTS[0]);
  const [leverage, setLeverage] = useState<LeverageStep>(LEVERAGE_STEPS[0]);

  useEffect(() => {
    if (!config) return;
    setAllocPct(toAllocationPct(config.capital_allocation_pct));
    setLeverage(toLeverageStep(config.leverage));
  }, [config]);

  const margin = useMemo(
    () => (walletUsd === null ? null : allocatedMargin(walletUsd, allocPct)),
    [walletUsd, allocPct],
  );
  const notional = useMemo(
    () => (walletUsd === null ? null : targetNotional(walletUsd, allocPct, leverage)),
    [walletUsd, allocPct, leverage],
  );

  const dirty =
    !!config &&
    (allocPct !== Number(config.capital_allocation_pct) || leverage !== Number(config.leverage));

  async function onSave(e: React.FormEvent) {
    e.preventDefault();
    try {
      // Exactly the two independent fields. No capital figure is computed and
      // saved: the executor multiplies these by the live wallet balance itself.
      await update.mutateAsync({ capital_allocation_pct: allocPct, leverage });
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
            {/* The capital base. Read-only, because it is not ours to set: it is
                whatever this client's own Binance futures wallet holds. */}
            <div className="rounded-lg border border-border bg-card/40 p-4">
              <div className="flex items-center justify-between gap-4">
                <span className="flex items-center gap-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">
                  <Wallet className="h-3.5 w-3.5" /> Binance wallet
                </span>
                <span className="font-mono text-lg font-semibold">
                  {walletPending ? "…" : walletUsd === null ? "—" : fmtUSD(walletUsd)}
                </span>
              </div>
              <p className="mt-2 text-xs text-muted-foreground">
                {walletUsd !== null ? (
                  <>
                    Your connected Binance USD-M wallet balance, read from the
                    exchange. Every order is sized from this figure — there is no
                    manual account size.
                    {wallet.state === "connected" && wallet.stale
                      ? " This reading is more than a few minutes old."
                      : null}
                  </>
                ) : wallet.state === "awaiting_read" ? (
                  "Keys are connected; waiting for the executor's first signed read of your account."
                ) : (
                  "No Binance account connected yet. Connect one to size orders — the engine will not open a position without a real wallet reading."
                )}
              </p>
            </div>

            <StepSelector
              label="Capital allocation"
              readout={`${allocPct}%`}
              steps={ALLOCATION_PCTS}
              value={allocPct}
              onChange={setAllocPct}
              format={(v) => `${v}%`}
              hint="Percentage of your real wallet balance committed as margin. Independent of leverage — changing this never changes leverage."
            />

            <StepSelector
              label="Leverage"
              readout={`${leverage}×`}
              steps={LEVERAGE_STEPS}
              value={leverage}
              onChange={setLeverage}
              format={(v) => `${v}×`}
              hint="Multiplier applied to the allocated margin. Independent of allocation — changing this never changes allocation. The executor clamps it to the ETHUSDT exchange maximum."
            />

            <div className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span className="text-warning/90">
                Higher leverage massively increases both gains and losses. At{" "}
                {leverage}× a roughly{" "}
                <span className="font-mono font-semibold">
                  {liquidationPct(leverage).toFixed(2)}%
                </span>{" "}
                adverse move liquidates the position.
              </span>
            </div>

            <div className="grid gap-3 rounded-lg border border-border bg-card/40 p-4 text-xs md:grid-cols-4">
              <KV k="Wallet balance" v={walletUsd === null ? "—" : fmtUSD(walletUsd)} />
              <KV k="Allocated margin" v={margin === null ? "—" : fmtUSD(margin)} />
              <KV k="Leverage" v={`${leverage}×`} />
              <KV k="Target notional" v={notional === null ? "—" : fmtUSD(notional)} />
            </div>

            <p className="text-xs text-muted-foreground">
              This target is what the executor asks Binance for. Nothing on our
              side reduces it: the only limits applied after this point are
              Binance&rsquo;s own leverage-bracket ceiling, its 0.001 lot step and
              its $20 minimum notional. If your account cannot post the margin,
              the order is refused outright rather than quietly made smaller.
            </p>

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
            Current: {config ? `${config.capital_allocation_pct}% alloc · ${config.leverage}×` : "—"}
          </span>
          <button type="submit" disabled={update.isPending || isLoading || !dirty}
            className="inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary to-accent px-4 py-2 text-sm font-semibold text-primary-foreground disabled:opacity-60">
            {update.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />} Save
          </button>
        </div>
      </form>

      <LiveExecutionSection config={config} />
    </div>
  );
}

/** A slider restricted to a fixed list of values. The slider moves over the
 *  INDEX, so no value between two adjacent steps is representable and the
 *  control cannot produce a number the server would reject. The tick labels
 *  below it are also buttons, so any permitted value is one click away. */
function StepSelector<T extends number>({
  label,
  readout,
  steps,
  value,
  onChange,
  format,
  hint,
}: {
  label: string;
  readout: string;
  steps: readonly T[];
  value: T;
  onChange: (v: T) => void;
  format: (v: T) => string;
  hint: string;
}) {
  const index = Math.max(0, steps.indexOf(value));
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
          {label}
        </span>
        <span className="font-mono text-lg font-semibold text-primary">{readout}</span>
      </div>
      <Slider
        className="mt-3"
        min={0}
        max={steps.length - 1}
        step={1}
        value={[index]}
        onValueChange={(v) => onChange(steps[Math.min(steps.length - 1, Math.max(0, v[0]))])}
        aria-label={label}
      />
      <div className="mt-2 flex flex-wrap justify-between gap-1 font-mono text-[10px] text-muted-foreground">
        {steps.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => onChange(s)}
            aria-pressed={s === value}
            className={`rounded px-1 py-0.5 transition-colors ${
              s === value ? "font-semibold text-primary" : "hover:text-foreground"
            }`}
          >
            {format(s)}
          </button>
        ))}
      </div>
      <p className="mt-2 text-xs text-muted-foreground">{hint}</p>
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
  // configured displays as OFF / $0 / auto disabled.
  const [execMode, setExecMode] = useState<RequestedExecutionMode>("OFF");
  const [autoExecute, setAutoExecute] = useState(false);

  useEffect(() => {
    if (!config) return;
    const stored = config.execution_mode;
    setExecMode(
      (REQUESTED_EXECUTION_MODES as readonly string[]).includes(stored)
        ? (stored as RequestedExecutionMode)
        : "OFF",
    );
    setAutoExecute(config.mode === "auto");
  }, [config]);

  const isLive = isRequestedLiveMode(execMode);

  // The same rule module the server and the database enforce, run against the
  // state this form would produce — so the UI refuses what the server would.
  const violations = useMemo(
    () =>
      validateLiveState({
        execution_mode: execMode,
        demo_mode: config?.demo_mode,
      }),
    [execMode, config?.demo_mode],
  );

  async function onSave(e: React.FormEvent) {
    e.preventDefault();
    if (violations.length > 0) {
      toast.error(formatViolations(violations));
      return;
    }
    try {
      await update.mutateAsync({
        execution_mode: execMode,
        mode: autoExecute ? "auto" : "signal_only",
      });
      toast.success("Live execution settings saved");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Save failed");
    }
  }

  const dirty =
    !!config &&
    (execMode !== config.execution_mode || autoExecute !== (config.mode === "auto"));

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

      {isLive && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            {execMode === "LIVE_TRADE"
              ? "Requesting real orders on Binance mainnet, sized from your own wallet balance, allocation and leverage. No per-order dollar ceiling is applied."
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
            ? `${config.execution_mode} · auto ${config.mode === "auto" ? "on" : "off"}`
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

function KV({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="uppercase tracking-wider text-muted-foreground">{k}</span>
      <span className="font-mono font-semibold">{v}</span>
    </div>
  );
}
