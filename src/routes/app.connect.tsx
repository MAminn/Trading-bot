import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { KeyRound, ShieldCheck, AlertTriangle, ExternalLink, Trash2, Loader2, Check } from "lucide-react";
import { toast } from "sonner";
import {
  saveBinanceKeys, getBinanceKeyInfo, deleteBinanceKeys,
} from "@/lib/binance.functions";

export const Route = createFileRoute("/app/connect")({
  head: () => ({ meta: [{ title: "Connect Binance — Helix" }] }),
  component: Connect,
});

function Connect() {
  const qc = useQueryClient();
  const fetchInfo = useServerFn(getBinanceKeyInfo);
  const saveFn = useServerFn(saveBinanceKeys);
  const deleteFn = useServerFn(deleteBinanceKeys);

  const info = useQuery({
    queryKey: ["binance", "info"],
    queryFn: () => fetchInfo({}),
  });

  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [note, setNote] = useState("trade-only, withdrawals disabled");
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState(false);

  async function onSave(e: React.FormEvent) {
    e.preventDefault();
    if (saving) return;
    setSaving(true);
    try {
      await saveFn({ data: { apiKey: apiKey.trim(), apiSecret: apiSecret.trim(), permissionsNote: note.trim() || undefined } });
      toast.success("Keys saved and encrypted.");
      setApiKey(""); setApiSecret("");
      qc.invalidateQueries({ queryKey: ["binance", "info"] });
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not save keys");
    } finally {
      setSaving(false);
    }
  }

  async function onDelete() {
    if (!confirm("Remove your Binance keys?")) return;
    setRemoving(true);
    try {
      await deleteFn({});
      toast.success("Keys removed.");
      qc.invalidateQueries({ queryKey: ["binance", "info"] });
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not remove keys");
    } finally {
      setRemoving(false);
    }
  }

  const connected = !!info.data?.api_key_last4;

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <div>
        <div className="text-xs uppercase tracking-[0.2em] text-primary">Exchange link</div>
        <h1 className="mt-2 font-display text-3xl font-semibold">Connect Binance</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
          Your API key + secret are encrypted with AES-256-GCM before they touch
          the database. The secret is never returned to the browser — only the
          last 4 chars of the public key are shown back to you.
        </p>
      </div>

      <div className="card-elevated border border-warning/30 bg-warning/5 p-5 text-sm">
        <div className="flex items-start gap-3">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
          <div className="space-y-1">
            <div className="font-medium text-warning">Trade-only keys — withdrawals must be DISABLED</div>
            <div className="text-muted-foreground">
              When you create the API key in Binance, do NOT enable the
              "Withdrawals" permission. Helix will never request it and the
              engine refuses to run with a key that has it enabled.
            </div>
            <a href="https://www.binance.com/en/support/faq/how-to-create-api-360002502072"
               target="_blank" rel="noreferrer"
               className="inline-flex items-center gap-1 text-xs text-primary hover:underline">
              How to create a Binance API key <ExternalLink className="h-3 w-3" />
            </a>
          </div>
        </div>
      </div>

      {connected && (
        <div className="card-elevated p-6">
          <div className="flex items-center gap-2 text-sm font-medium">
            <ShieldCheck className="h-4 w-4 text-success" /> Connected
          </div>
          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <KV k="API key" v={`••••${info.data!.api_key_last4}`} />
            <KV k="Permissions note" v={info.data!.permissions_note ?? "—"} />
            <KV k="Saved" v={new Date(info.data!.created_at).toLocaleString()} />
            <KV k="Updated" v={new Date(info.data!.updated_at).toLocaleString()} />
          </div>
          <button onClick={onDelete} disabled={removing}
            className="mt-5 inline-flex items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm font-medium text-destructive hover:bg-destructive/20 disabled:opacity-60">
            {removing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />} Remove keys
          </button>
        </div>
      )}

      <form onSubmit={onSave} className="card-elevated space-y-5 p-6">
        <div className="flex items-center gap-2 text-sm font-medium">
          <KeyRound className="h-4 w-4 text-primary" />
          {connected ? "Replace keys" : "Save keys"}
        </div>
        <Field label="API key" value={apiKey} onChange={setApiKey} placeholder="64-char Binance API key" />
        <Field label="API secret" value={apiSecret} onChange={setApiSecret} placeholder="64-char Binance API secret" type="password" />
        <Field label="Permissions note" value={note} onChange={setNote} placeholder="trade-only, withdrawals disabled" />

        <div className="flex items-center justify-end gap-3">
          <button type="submit" disabled={saving || apiKey.length < 16 || apiSecret.length < 16}
            className="inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary to-accent px-4 py-2 text-sm font-semibold text-primary-foreground disabled:opacity-60">
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
            {saving ? "Encrypting…" : "Save encrypted"}
          </button>
        </div>
      </form>
    </div>
  );
}

function Field({ label, value, onChange, type = "text", placeholder }: { label: string; value: string; onChange: (v: string) => void; type?: string; placeholder?: string }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium uppercase tracking-wider text-muted-foreground">{label}</span>
      <input
        type={type} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder}
        className="w-full rounded-lg border border-border bg-input/40 px-3 py-2.5 font-mono text-sm focus:border-primary/60 focus:outline-none focus:ring-2 focus:ring-primary/20"
      />
    </label>
  );
}

function KV({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-center justify-between rounded-lg border border-border bg-card/40 px-4 py-3">
      <span className="text-xs uppercase tracking-wider text-muted-foreground">{k}</span>
      <span className="font-mono text-sm">{v}</span>
    </div>
  );
}
