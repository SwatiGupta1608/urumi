import { useState, type ReactNode } from "react";
import type { ApiError } from "../api/client";

export function Loading({ label = "Loading…" }: { label?: string }) {
  return <p className="muted loading" role="status" aria-live="polite">{label}</p>;
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

type Theme = "auto" | "light" | "dark";
const THEMES: { id: Theme; label: string }[] = [{ id: "auto", label: "Auto" }, { id: "light", label: "Light" }, { id: "dark", label: "Dark" }];

function applyTheme(t: Theme) {
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
}

/** Light/dark switch. "Auto" follows the OS. Held only on <html data-theme>: nothing is written to browser storage. */
export function ThemeToggle({ className = "" }: { className?: string }) {
  const [theme, setTheme] = useState<Theme>(() => (document.documentElement.getAttribute("data-theme") as Theme | null) ?? "auto");
  function choose(t: Theme) {
    applyTheme(t);
    setTheme(t);
  }
  return (
    <div className={`theme-toggle ${className}`} role="group" aria-label="Theme">
      {THEMES.map((t) => (
        <button key={t.id} type="button" aria-pressed={theme === t.id} onClick={() => choose(t.id)}>{t.label}</button>
      ))}
    </div>
  );
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
