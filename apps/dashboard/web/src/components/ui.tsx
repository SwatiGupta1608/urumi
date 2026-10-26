import { useState, type ReactNode } from "react";
import type { ApiError } from "../api/client";

export function Loading({ label = "Loading…" }: { label?: string }) {
  return <p className="muted" role="status" aria-live="polite">{label}</p>;
}

export function ErrorBanner({ error, onRetry }: { error: ApiError | Error | string; onRetry?: () => void }) {
  const msg = typeof error === "string" ? error : error.message;
  const status = typeof error === "object" && "status" in error ? (error as ApiError).status : undefined;
  return (
    <div className="banner error" role="alert">
      <span>{status === 403 ? `Not permitted: ${msg}` : msg}</span>
      {onRetry && <button className="link" onClick={onRetry}>Retry</button>}
    </div>
  );
}

export function Notice({ children, kind = "info" }: { children: ReactNode; kind?: "info" | "success" | "warning" }) {
  return <div className={`banner ${kind}`} role="status">{children}</div>;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function Badge({ tone, children }: { tone: "ok" | "bad" | "warn" | "neutral"; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function ReachBadge({ enabled, reachable }: { enabled: boolean; reachable: boolean | null }) {
  if (!enabled) return <Badge tone="neutral">disabled</Badge>;
  if (reachable === null) return <Badge tone="warn">not checked</Badge>;
  return reachable ? <Badge tone="ok">reachable</Badge> : <Badge tone="bad">unreachable</Badge>;
}

export function Time({ value }: { value: string | null }) {
  if (!value) return <span className="muted">—</span>;
  const d = new Date(value);
  return <time dateTime={value} title={d.toISOString()}>{d.toLocaleString()}</time>;
}

/** Two-step button for destructive actions. */
export function ConfirmButton({ label, confirmLabel = "Confirm", onConfirm, disabled, className = "danger" }:
  { label: string; confirmLabel?: string; onConfirm: () => void | Promise<void>; disabled?: boolean; className?: string }) {
  const [armed, setArmed] = useState(false);
  if (!armed) return <button className={className} disabled={disabled} onClick={() => setArmed(true)}>{label}</button>;
  return (
    <span className="confirm">
      <button className={className} disabled={disabled} onClick={async () => { setArmed(false); await onConfirm(); }}>
        {confirmLabel}
      </button>
      <button className="link" onClick={() => setArmed(false)}>Cancel</button>
    </span>
  );
}
