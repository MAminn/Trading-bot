import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";
import { Play, Square, Activity, Copy, Check, AlertTriangle, Cpu, FlaskConical } from "lucide-react";
import { supabase } from "@/integrations/supabase/client";
import { useQuery } from "@tanstack/react-query";
import {
  useEngineStatus, useEngineConfig, useSetRunning, useUpdateConfig, liveState, fmtAgo, fmtUSD,
} from "@/lib/engine";
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

function EnginePage() {
  const status = useEngineStatus();
  const config = useEngineConfig();
  const setRunning = useSetRunning();
  const updateConfig = useUpdateConfig();
  const { data: userId } = useUserId();
  const state = liveState(status.data, !!config.data?.is_running);
  const isRunning = !!config.data?.is_running;
  const demoMode = !!config.data?.demo_mode;

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

      {/* Status detail */}
      <div className="card-elevated p-6">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Activity className="h-4 w-4 text-primary" /> Engine status
        </div>
        <div className="mt-5 grid gap-4 md:grid-cols-2">
          <KV k="State" v={state.toUpperCase()} tone={state === "running" ? "success" : state === "error" ? "destructive" : state === "stale" ? "warn" : undefined} />
          <KV k="Worker says" v={status.data?.status ?? "—"} />
          <KV k="Last heartbeat" v={fmtAgo(status.data?.last_heartbeat)} />
          <KV k="Current position" v={status.data?.current_position ?? "FLAT"} />
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
        {config.data ? (
          <div className="mt-5 grid gap-4 md:grid-cols-3">
            <KV k="Mode" v={config.data.mode} />
            <KV k="Strategy capital" v={fmtUSD(Number(config.data.capital_usd))} />
            <KV k="Allocation" v={`${config.data.capital_allocation_pct ?? 100}%`} />
            <KV k="Leverage" v={`${config.data.leverage}×`} />
            <KV k="Max daily loss" v={fmtUSD(Number(config.data.max_daily_loss_usd))} />
            <KV k="Max position size" v={fmtUSD(Number(config.data.max_position_size_usd))} />
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
