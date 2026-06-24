// Live alerts: drives toasts + browser notifications from the engine's
// user_signals table via realtime + initial backlog. No random simulation.

import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from "react";
import { toast } from "sonner";
import { supabase } from "@/integrations/supabase/client";
import { type SignalRow, signalSideLabel } from "@/lib/engine";

export type AlertKind = "buy_signal" | "sell_signal" | "risk_breach" | "info";

export interface AlertItem {
  id: string;
  kind: AlertKind;
  title: string;
  message: string;
  meta?: string;
  createdAt: number;
  read: boolean;
}

interface AlertsContextValue {
  alerts: AlertItem[];
  unreadCount: number;
  browserPermission: NotificationPermission | "unsupported";
  requestBrowserPermission: () => Promise<void>;
  markAllRead: () => void;
  clear: () => void;
  push: (alert: Omit<AlertItem, "id" | "createdAt" | "read">) => void;
}

const AlertsContext = createContext<AlertsContextValue | null>(null);
const STORAGE_KEY = "helix.alerts.v1";
const MAX_ALERTS = 50;

function signalToAlert(s: SignalRow): Omit<AlertItem, "read"> {
  const conf = s.ml_prob != null ? Number(s.ml_prob).toFixed(2) : "—";
  const side = signalSideLabel(s);
  const ts = new Date(s.bar_time ?? s.created_at).getTime();
  if (s.ml_accept === false || (s.rule_side && !s.valid_next_entry)) {
    return {
      id: `a_${s.id}`,
      createdAt: ts,
      kind: "risk_breach",
      title: `${side} vetoed · ${s.rule_reason ?? "filter"}`,
      message: `Confidence ${conf} · threshold ${s.ml_threshold ?? "—"}`,
      meta: s.opened ?? undefined,
    };
  }
  const kind: AlertKind = side === "LONG" ? "buy_signal" : side === "SHORT" ? "sell_signal" : "info";
  return {
    id: `a_${s.id}`,
    createdAt: ts,
    kind,
    title: `${side} signal · ${s.rule_reason ?? "engine"}`,
    message: `Confidence ${conf} · threshold ${s.ml_threshold ?? "—"}`,
    meta: s.opened ?? undefined,
  };
}

export function AlertsProvider({ children }: { children: ReactNode }) {
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [browserPermission, setBrowserPermission] = useState<NotificationPermission | "unsupported">(
    typeof window !== "undefined" && "Notification" in window ? Notification.permission : "unsupported",
  );
  const hydrated = useRef(false);
  const seenIds = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const parsed: AlertItem[] = JSON.parse(raw);
        setAlerts(parsed);
        parsed.forEach((a) => seenIds.current.add(a.id));
      }
    } catch { /* ignore */ }
    hydrated.current = true;
  }, []);

  useEffect(() => {
    if (!hydrated.current || typeof window === "undefined") return;
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(alerts.slice(0, MAX_ALERTS))); }
    catch { /* ignore */ }
  }, [alerts]);

  const fireBrowserNotification = useCallback((alert: AlertItem) => {
    if (typeof window === "undefined" || !("Notification" in window)) return;
    if (Notification.permission !== "granted") return;
    try { new Notification(alert.title, { body: alert.message, tag: alert.id }); }
    catch { /* ignore */ }
  }, []);

  const ingest = useCallback((alert: AlertItem, options?: { silent?: boolean }) => {
    if (seenIds.current.has(alert.id)) return;
    seenIds.current.add(alert.id);
    setAlerts((prev) => [alert, ...prev].slice(0, MAX_ALERTS));
    if (options?.silent) return;
    const sonnerOpts = { description: alert.message, duration: 6000 };
    switch (alert.kind) {
      case "buy_signal": toast.success(alert.title, sonnerOpts); break;
      case "sell_signal": toast.error(alert.title, sonnerOpts); break;
      case "risk_breach": toast.warning(alert.title, sonnerOpts); break;
      default: toast(alert.title, sonnerOpts);
    }
    fireBrowserNotification(alert);
  }, [fireBrowserNotification]);

  const push = useCallback<AlertsContextValue["push"]>((input) => {
    const alert: AlertItem = {
      id: `m_${Date.now()}_${alerts.length}`,
      createdAt: Date.now(),
      read: false,
      ...input,
    };
    ingest(alert);
  }, [alerts.length, ingest]);

  const requestBrowserPermission = useCallback(async () => {
    if (typeof window === "undefined" || !("Notification" in window)) return;
    const result = await Notification.requestPermission();
    setBrowserPermission(result);
    if (result === "granted") {
      push({ kind: "info", title: "Browser alerts enabled", message: "You'll get a system notification on every engine signal." });
    }
  }, [push]);

  const markAllRead = useCallback(() => {
    setAlerts((prev) => prev.map((a) => ({ ...a, read: true })));
  }, []);

  const clear = useCallback(() => { seenIds.current = new Set(); setAlerts([]); }, []);

  useEffect(() => {
    let active = true;
    (async () => {
      const { data } = await supabase
        .from("user_signals").select("*")
        .order("created_at", { ascending: false }).limit(20);
      if (!active || !data) return;
      [...data].reverse().forEach((row) => {
        const a = { ...signalToAlert(row as SignalRow), read: true };
        ingest(a, { silent: true });
      });
    })();

    const channel = supabase
      .channel("alerts-signals")
      .on("postgres_changes",
        { event: "INSERT", schema: "public", table: "user_signals" },
        (payload) => {
          const a = { ...signalToAlert(payload.new as SignalRow), read: false };
          ingest(a);
        })
      .subscribe();

    return () => { active = false; supabase.removeChannel(channel); };
  }, [ingest]);

  const unreadCount = useMemo(() => alerts.filter((a) => !a.read).length, [alerts]);

  const value: AlertsContextValue = {
    alerts, unreadCount, browserPermission, requestBrowserPermission, markAllRead, clear, push,
  };

  return <AlertsContext.Provider value={value}>{children}</AlertsContext.Provider>;
}

export function useAlerts() {
  const ctx = useContext(AlertsContext);
  if (!ctx) throw new Error("useAlerts must be used within AlertsProvider");
  return ctx;
}

export function formatRelative(ts: number) {
  const diff = Math.max(0, Date.now() - ts);
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}
