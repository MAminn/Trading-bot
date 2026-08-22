import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";
import {
  Play, Square, Activity, Copy, Check, AlertTriangle, Cpu, FlaskConical, Radio, ShieldAlert, Info,
} from "lucide-react";
import { supabase } from "@/integrations/supabase/client";
import { useQuery } from "@tanstack/react-query";
import {
  useEngineStatus, useEngineConfig, useSetRunning, useUpdateConfig, liveState, fmtAgo, fmtUSD,
} from "@/lib/engine";
import {
  useExecutorStatus, executorFresh, executorTone, canPlaceOrders, isLiveMode,
  MODE_LABEL, PERMISSION_LABEL, type ExecutorStatusRow,
} from "@/lib/executor";
import { getBinanceKeyInfo } from "@/lib/binance.functions";
import { useServerFn } from "@tanstack/react-start";
import { Switch } from "@/components/ui/switch";
import { toast } from "sonner";

export const Route = createFileRoute("/app/engine")({
  head: () => ({ meta: [{ title: "Engine — Helix" }] }),
  component: EnginePage,
});

function useUserId() {
  return useQuery({
    queryKey: ["auth", "uid"],
    queryFn: async () => {
      const { data } = await supabase.auth.getUser();
      return data.user?.id ?? null;
    },
    staleTime: 60_000,
  });
}

/** The connected key's last 4 characters, for display.
 *
 *  Deliberately routed through getBinanceKeyInfo rather than through the
 *  executor's telemetry: that function is RLS-scoped to the signed-in user and
 *  returns last4 plus timestamps, so this page cannot render a secret even if a
 *  later change tried to. The plaintext key never exists in the browser at all
 *  — the one place it is decrypted is the executor's credentials endpoint,
 *  server-side, and that response never reaches a page.
 */
function useBinanceKeyInfo() {
  const fetchInfo = useServerFn(getBinanceKeyInfo);
  return useQuery({
    queryKey: ["binance", "key-info"],
    queryFn: () => fetchInfo(),
    staleTime: 60_000,
  });
}

function EnginePage() {
  const status = useEngineStatus();
  const config = useEngineConfig();
  const executor = useExecutorStatus();
  const setRunning = useSetRunning();
  const updateConfig = useUpdateConfig();
  const { data: userId } = useUserId();
  const keyInfo = useBinanceKeyInfo();
  const state = liveState(status.data, !!config.data?.is_running);
  const isRunning = !!config.data?.is_running;
  const demoMode = !!config.data?.demo_mode;
  const isFullCapital = config.data?.sizing_mode === "full_capital";
  // account_size_usd is nullable on rows written before the sizing migration.
  const rawAccountSize = Number(config.data?.account_size_usd);
  const accountSize = Number.isFinite(rawAccountSize) ? rawAccountSize : null;
  // What the engine aims for before caps: full capital sizes off the account,
  // allocation off the allocated slice of it.
  const sizingBase = isFullCapital ? accountSize : Number(config.data?.capital_usd);
  const targetNotional =
    config.data && sizingBase !== null && Number.isFinite(sizingBase)
      ? sizingBase * Number(config.data.leverage)
      : null;

  async function toggle() {
    try {
      await setRunning.mutateAsync(!isRunning);
      toast.success(!isRunning ? "Engine starting…" : "Engine stopping…");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed");
    }
  }

  async function toggleDemo(next: boolean) {
    try {
      await updateConfig.mutateAsync({ demo_mode: next });
      toast.success(next ? "Demo mode ON — simulated data will start flowing" : "Demo mode OFF");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed");
    }
  }

  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const baseUrl = `${origin}/api/public/engine`;

  return (
    <div className="mx-auto max-w-5xl space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-[0.2em] text-primary">Engine</div>
          <h1 className="mt-2 font-display text-3xl font-semibold">ETHUSDT Engine</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Flip the Start/Stop flag the external Python engine polls. Live data
            flows from the engine into this dashboard via secure endpoints.
          </p>
        </div>
        <button
          onClick={toggle}
          disabled={setRunning.isPending}
          className={`inline-flex items-center gap-2 rounded-lg px-5 py-2.5 text-sm font-semibold disabled:opacity-60 ${
            isRunning ? "bg-destructive text-destructive-foreground" : "bg-gradient-to-r from-primary to-accent text-primary-foreground"
          }`}>
          {isRunning ? <Square className="h-4 w-4" /> : <Play className="h-4 w-4" />}
          {isRunning ? "Stop engine" : "Start engine"}
        </button>
      </div>

      {/* Executor: the real Binance-facing process */}
      <ExecutorCard
        row={executor.data}
        loading={executor.isLoading}
        requested={config.data?.execution_mode}
        keyLast4={keyInfo.data?.api_key_last4 ?? null}
      />

      {/* ML signal worker status — the strategy's own view, NOT the exchange's */}
      <div className="card-elevated p-6">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Activity className="h-4 w-4 text-primary" /> ML signal worker
        </div>
        <p className="mt-2 max-w-2xl text-xs text-muted-foreground">
          The strategy process that produces signals. Its position is the
          strategy's own bookkeeping — the exchange's real position is in the
          executor card above.
        </p>
        <div className="mt-5 grid gap-4 md:grid-cols-2">
          <KV k="State" v={state.toUpperCase()} tone={state === "running" ? "success" : state === "error" ? "destructive" : state === "stale" ? "warn" : undefined} />
          <KV k="Worker says" v={status.data?.status ?? "—"} />
          <KV k="Last heartbeat" v={fmtAgo(status.data?.last_heartbeat)} />
          <KV k="Strategy position" v={status.data?.current_position ?? "FLAT"} />
          <KV k="Is running flag" v={isRunning ? "TRUE" : "FALSE"} tone={isRunning ? "success" : undefined} />
          <KV k="Last message" v={status.data?.message ?? "—"} />
        </div>
      </div>

      {/* Demo / TEST mode */}
      <div className={`card-elevated p-6 ${demoMode ? "ring-1 ring-warning/50" : ""}`}>
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2 text-sm font-medium">
              <FlaskConical className={`h-4 w-4 ${demoMode ? "text-warning" : "text-muted-foreground"}`} /> Demo / Test mode
            </div>
            <p className="mt-2 max-w-xl text-sm text-muted-foreground">
              Generates <strong>simulated</strong> signals, positions and trades
              every ~1 minute so you can verify the dashboard end-to-end before
              connecting the real Python engine. Data is clearly tagged{" "}
              <code className="rounded bg-card px-1 py-0.5 font-mono text-xs">[DEMO]</code>{" "}
              and never touches Binance. Turn this off before going live.
            </p>
          </div>
          <Switch
            checked={demoMode}
            onCheckedChange={toggleDemo}
            disabled={updateConfig.isPending}
            aria-label="Toggle demo mode"
          />
        </div>
        {demoMode && (
          <div className="mt-4 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
            DEMO MODE is ON — a persistent warning banner is visible on every page.
          </div>
        )}
      </div>

      {/* Active config */}
      <div className="card-elevated p-6">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Cpu className="h-4 w-4 text-primary" /> Active configuration
        </div>
        {isLiveMode(executor.data?.effective_mode) && (
          <p className="mt-2 max-w-2xl text-xs text-muted-foreground">
            These are sizing inputs only. What the executor is permitted to do
            is set in its own environment on the VPS and is shown in the
            executor card above — not by the signal mode below.
          </p>
        )}
        {config.data ? (
          <div className="mt-5 grid gap-4 md:grid-cols-3">
            {/* Labelled as the app-side signal mode: it does NOT gate live
                execution, and reading it as "signal only" while the executor
                runs LIVE_TRADE is exactly the confusion to avoid. */}
            <KV k="Signal mode (app)" v={config.data.mode} />
            <KV k="Strategy capital" v={fmtUSD(Number(config.data.capital_usd))} />
            <KV
              k="Allocation"
              v={isFullCapital ? "n/a" : `${config.data.capital_allocation_pct ?? 100}%`}
            />
            <KV k="Leverage" v={`${config.data.leverage}×`} />
            <KV k="Sizing mode" v={isFullCapital ? "FULL CAPITAL" : "ALLOCATION"} tone={isFullCapital ? "warn" : undefined} />
            <KV k="Account size" v={accountSize === null ? "—" : fmtUSD(accountSize)} />
            <KV k="Target notional" v={targetNotional === null ? "—" : fmtUSD(targetNotional)} />
            <KV k="Updated" v={fmtAgo(config.data.updated_at)} />
          </div>
        ) : (
          <div className="mt-4 text-sm text-muted-foreground">Loading…</div>
        )}
      </div>

      {/* Engine wiring info */}
      <div className="card-elevated p-6">
        <div className="flex items-center gap-2 text-sm font-medium">
          <AlertTriangle className="h-4 w-4 text-warning" /> Wire the Python engine
        </div>
        <p className="mt-3 text-sm text-muted-foreground">
          Point your external Python engine at these endpoints. It needs to send
          <code className="mx-1 rounded bg-card px-1.5 py-0.5 font-mono text-xs">Authorization: Bearer {"<ENGINE_SERVICE_TOKEN>"}</code>
          on every request.
        </p>
        <div className="mt-5 space-y-3">
          <CopyRow label="Heartbeat (POST)" value={`${baseUrl}/heartbeat`} />
          <CopyRow label="Ingest signal (POST)" value={`${baseUrl}/ingest/signal`} />
          <CopyRow label="Ingest trade (POST)" value={`${baseUrl}/ingest/trade`} />
          <CopyRow label="Fetch config + keys (GET)" value={`${baseUrl}/config?user_id=${userId ?? "<your-user-id>"}`} />
          <CopyRow label="Your user_id" value={userId ?? "—"} />
        </div>
        <p className="mt-4 text-xs text-muted-foreground">
          The service token is provisioned as an environment variable
          (<code className="font-mono">ENGINE_SERVICE_TOKEN</code>) in your backend.
          Copy its value from your project secrets and paste it into the engine's
          environment. The token is never returned to the browser.
        </p>
      </div>
    </div>
  );
}

function ExecutorCard({
  row,
  loading,
  requested,
  keyLast4,
}: {
  row: ExecutorStatusRow | null | undefined;
  loading: boolean;
  /** What the database has been asked for. The executor does not read this
   *  yet, so a difference from effective_mode is expected, not an alarm. */
  requested: string | null | undefined;
  /** Last 4 characters of the connected Binance API key. Never the key itself
   *  and never the secret — this is the only key-derived value the browser is
   *  ever given. */
  keyLast4: string | null;
}) {
  const fresh = executorFresh(row);
  const tone = executorTone(row);
  const ring =
    tone === "live" ? "ring-1 ring-destructive/50" : tone === "warn" ? "ring-1 ring-warning/40" : "";

  if (loading) {
    return (
      <div className="card-elevated p-6">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Radio className="h-4 w-4 text-primary" /> Executor (Binance)
        </div>
        <div className="mt-4 text-sm text-muted-foreground">Loading…</div>
      </div>
    );
  }

  // No row at all: either the executor has never reported, or this app is
  // running ahead of the migration. Say so plainly rather than implying the
  // executor is idle — an unreported executor may well be trading.
  if (!row) {
    return (
      <div className="card-elevated p-6">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Radio className="h-4 w-4 text-muted-foreground" /> Executor (Binance)
        </div>
        <div className="mt-4 flex items-start gap-3 rounded-lg border border-border bg-card/40 p-4 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <div>
            <div className="font-medium">No executor telemetry</div>
            <p className="mt-0.5 text-muted-foreground">
              The executor has not reported yet. This does <strong>not</strong>{" "}
              mean it is stopped — check the executor host directly before
              assuming anything about live execution.
              {requested ? (
                <>
                  {" "}
                  The database currently requests{" "}
                  <span className="font-mono text-foreground">{requested}</span>, which
                  says nothing about what the executor is running.
                </>
              ) : null}
            </p>
          </div>
        </div>
      </div>
    );
  }

  const modeLabel = MODE_LABEL[row.effective_mode] ?? row.effective_mode;
  const modeColor =
    tone === "live" ? "text-destructive" : tone === "warn" ? "text-warning" : "text-muted-foreground";
  const num = (n: number | null, digits = 3) => (n === null ? "—" : n.toFixed(digits));

  return (
    <div className={`card-elevated p-6 ${ring}`}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Radio className={`h-4 w-4 ${modeColor}`} /> Executor (Binance)
        </div>
        <div className="flex items-center gap-2">
          <span className={`rounded-full border px-3 py-1 font-mono text-xs font-semibold ${
            tone === "live"
              ? "border-destructive/50 bg-destructive/10 text-destructive"
              : tone === "warn"
                ? "border-warning/50 bg-warning/10 text-warning"
                : "border-border bg-card/60 text-muted-foreground"
          }`}>
            {modeLabel}
          </span>
          {!fresh && (
            <span className="rounded-full border border-warning/50 bg-warning/10 px-3 py-1 text-xs font-medium text-warning">
              STALE · {fmtAgo(row.last_heartbeat)}
            </span>
          )}
        </div>
      </div>

      {canPlaceOrders(row.effective_mode) && (
        <div className="mt-4 flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
          <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            <strong>This executor can place orders.</strong>{" "}
            {row.effective_mode === "LIVE_TRADE"
              ? "Mode is LIVE_TRADE against Binance mainnet — orders use real funds."
              : "Mode is TESTNET_TRADE — orders are placed on the Binance testnet."}
          </span>
        </div>
      )}

      {!fresh && (
        <div className="mt-4 flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 p-3 text-xs text-warning">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            No executor heartbeat for {fmtAgo(row.last_heartbeat)}. The values
            below are the last known snapshot and may no longer be true —
            including the mode.
          </span>
        </div>
      )}

      {/* Requested vs actual. These are two different facts and the page must
          never let them be read as one: the database records what was asked
          for, executor_status records what is running. */}
      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <div className="rounded-lg border border-border bg-card/40 px-4 py-3">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
            Requested (database)
          </div>
          <div className="mt-1 font-mono text-sm">{requested ?? "—"}</div>
        </div>
        <div
          className={`rounded-lg border px-4 py-3 ${
            tone === "live"
              ? "border-destructive/50 bg-destructive/10"
              : tone === "warn"
                ? "border-warning/50 bg-warning/10"
                : "border-border bg-card/40"
          }`}
        >
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
            Actual (executor)
          </div>
          <div className={`mt-1 font-mono text-sm font-semibold ${modeColor}`}>
            {row.effective_mode}
          </div>
        </div>
      </div>

      {/* The executor now reports the request it saw, so prefer its value over
          the one this page read from the database — they are the same row, but
          the executor's is the one it actually acted on. */}
      {(() => {
        const seen = row.db_execution_mode ?? requested ?? null;
        if (seen === null || seen === row.effective_mode) return null;
        return (
          <div className="mt-3 flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 p-3 text-xs">
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
            <span className="text-warning">
              Requested <span className="font-mono">{seen}</span>, running{" "}
              <span className="font-mono">{row.effective_mode}</span>. The host's{" "}
              <span className="font-mono">.env</span> ceiling is{" "}
              <span className="font-mono">{row.env_mode_ceiling ?? "unknown"}</span> and it
              cannot be raised from this dashboard. The{" "}
              <strong>running</strong> value is the one that describes real behaviour.
            </span>
          </div>
        );
      })()}

      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <KV
          k="Orders enabled"
          v={row.orders_enabled === null ? "—" : row.orders_enabled ? "YES" : "NO"}
          tone={row.orders_enabled ? "destructive" : undefined}
        />
        <KV
          k="Auto-execute"
          v={row.auto_execute_enabled === null ? "—" : row.auto_execute_enabled ? "ON" : "OFF"}
        />
        <KV
          k="Effective order cap"
          v={row.live_order_cap_usd === null ? "—" : fmtUSD(row.live_order_cap_usd)}
        />
        <KV
          k="Host cap ceiling"
          v={row.live_order_cap_env_max === null ? "—" : fmtUSD(row.live_order_cap_env_max)}
        />
      </div>

      {row.orders_enabled === false && row.blocked_reason && (
        <div className="mt-3 rounded-lg border border-border bg-card/40 px-4 py-3 text-xs">
          <span className="text-muted-foreground">OPENs blocked by </span>
          <span className="font-mono">{row.blocked_reason}</span>
          <span className="text-muted-foreground">
            {" "}
            — closing an existing position is still permitted while the effective
            mode is trade-capable.
          </span>
        </div>
      )}

      <div className="mt-5 grid gap-4 md:grid-cols-2">
        <KV k="Env ceiling" v={row.env_mode_ceiling ?? "—"} />
        <KV
          k="Wallet balance (Binance)"
          v={row.wallet_balance_usd === null ? "not read yet" : fmtUSD(row.wallet_balance_usd)}
          tone={row.wallet_balance_usd === null ? "warn" : undefined}
        />
        <KV
          k="Available balance (Binance)"
          v={row.available_balance_usd === null ? "not read yet" : fmtUSD(row.available_balance_usd)}
          tone={row.available_balance_usd === null ? "warn" : undefined}
        />
        <KV k="Binance position" v={row.position_side ?? "—"} tone={row.position_side && row.position_side !== "FLAT" ? "warn" : undefined} />
        <KV k="Position size" v={num(row.position_amt)} />
        <KV k="Entry price" v={row.entry_price === null || row.entry_price === 0 ? "—" : fmtUSD(row.entry_price)} />
        <KV k="Position leverage" v={row.position_leverage === null ? "—" : `${row.position_leverage}×`} />
        <KV k="Margin type" v={row.margin_type ? row.margin_type.toUpperCase() : "—"} />
        <KV
          k="Connected key"
          v={keyLast4 ? "····" + keyLast4 : "not connected"}
          tone={keyLast4 ? undefined : "destructive"}
        />
        <KV k="Keys present" v={row.keys_present === null ? "—" : row.keys_present ? "YES" : "NO"} tone={row.keys_present === false ? "destructive" : undefined} />
        <KV
          k="Key permissions"
          v={row.permission_status ? PERMISSION_LABEL[row.permission_status] : "—"}
          tone={row.permission_status === "failed" ? "destructive" : row.permission_status === "unknown" ? "warn" : undefined}
        />
        <KV k="Last heartbeat" v={fmtAgo(row.last_heartbeat)} tone={fresh ? "success" : "warn"} />
      </div>

      <div className="mt-6">
        <div className="text-xs uppercase tracking-wider text-muted-foreground">Last reconcile</div>
        <div className="mt-3 grid gap-4 md:grid-cols-2">
          <KV
            k="Match"
            v={row.reconcile_match === null ? "—" : row.reconcile_match ? "MATCH" : "MISMATCH"}
            tone={row.reconcile_match === null ? undefined : row.reconcile_match ? "success" : "destructive"}
          />
          <KV k="When" v={fmtAgo(row.last_reconcile_at)} />
          <KV k="Expected" v={num(row.reconcile_expected)} />
          <KV k="Actual" v={num(row.reconcile_actual)} />
        </div>
        {row.reconcile_match === false && (
          <p className="mt-3 text-xs text-destructive">
            The app's expected position disagrees with Binance. The executor
            blocks new OPENs while this is true; CLOSEs remain allowed.
          </p>
        )}
      </div>

      {row.message && (
        <div className="mt-5 rounded-lg border border-border bg-card/40 px-4 py-3">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Executor says</div>
          <div className="mt-1 wrap-break-word font-mono text-xs">{row.message}</div>
        </div>
      )}
    </div>
  );
}

function KV({ k, v, tone }: { k: string; v: string; tone?: "success" | "destructive" | "warn" }) {
  const color = tone === "success" ? "text-success" : tone === "destructive" ? "text-destructive" : tone === "warn" ? "text-warning" : "";
  return (
    <div className="flex items-center justify-between rounded-lg border border-border bg-card/40 px-4 py-3">
      <span className="text-xs uppercase tracking-wider text-muted-foreground">{k}</span>
      <span className={`font-mono text-sm ${color}`}>{v}</span>
    </div>
  );
}

function CopyRow({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-border bg-card/40 px-4 py-3">
      <div className="min-w-0">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
        <div className="truncate font-mono text-xs">{value}</div>
      </div>
      <button
        type="button"
        onClick={async () => { await navigator.clipboard.writeText(value); setCopied(true); setTimeout(() => setCopied(false), 1200); }}
        className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 text-xs hover:bg-card/80"
      >
        {copied ? <Check className="h-3 w-3 text-success" /> : <Copy className="h-3 w-3" />}
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
