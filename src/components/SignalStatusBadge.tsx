import { STATUS_META, type SignalStatus } from "@/lib/engine";

export function SignalStatusBadge({ status, className = "" }: { status: SignalStatus; className?: string }) {
  const m = STATUS_META[status];
  return (
    <span className={`rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider ${m.cls} ${className}`}>
      {m.label}
    </span>
  );
}
