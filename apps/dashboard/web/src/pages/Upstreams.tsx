import { useState, type FormEvent } from "react";
import { api, ApiError } from "../api/client";
import type { Tool, Upstream, UpstreamPatch } from "../api/types";
import { Badge, ConfirmButton, EmptyState, ErrorBanner, Loading, Notice, ReachBadge, Time } from "../components/ui";
import { useAsync } from "../components/useAsync";

// Client-side checks mirror the gateway's validators; the gateway still validates everything.
const SLUG_RE = /^[a-z][a-z0-9]*(-[a-z0-9]+)*$/;
const CRED_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;
const TOOL_RE = /^[A-Za-z0-9_.-]{1,128}$/;

function parseTools(text: string): string[] | string {
  const list = text.split(",").map((t) => t.trim()).filter(Boolean);
  const bad = list.find((t) => !TOOL_RE.test(t));
  return bad ? `Invalid tool name: ${bad}` : list;
}

export function validateUpstream(f: { slug?: string; display_name: string; url: string; credential_ref: string; tools: string }): string | null {
  if (f.slug !== undefined && (!SLUG_RE.test(f.slug) || f.slug.length > 32))
    return "Slug: lowercase letters/digits, single hyphens, no underscores, max 32 chars.";
  if (!f.display_name.trim()) return "Display name is required.";
  try {
    const u = new URL(f.url);
    if (!["http:", "https:"].includes(u.protocol)) return "URL must start with http:// or https://.";
  } catch {
    return "URL is not valid.";
  }
  if (f.credential_ref && !CRED_RE.test(f.credential_ref)) return "Credential reference: lowercase letters, digits and hyphens.";
  const t = parseTools(f.tools);
  return typeof t === "string" ? t : null;
}

export function Upstreams() {
  const ups = useAsync(() => api.upstreams());
  const tools = useAsync(() => api.tools());
  const [flash, setFlash] = useState<string>();
  const [actionError, setActionError] = useState<ApiError>();

  async function act(fn: () => Promise<unknown>, ok: string): Promise<boolean> {
    setActionError(undefined);
    setFlash(undefined);
    try {
      await fn();
      setFlash(ok);
      return true;
    } catch (e) {
      setActionError(e as ApiError);
      return false;
    } finally {
      await Promise.all([ups.reload(), tools.reload()]);
    }
  }

  const byUpstream = new Map<string, Tool[]>();
  (tools.data ?? []).forEach((t) => byUpstream.set(t.upstream, [...(byUpstream.get(t.upstream) ?? []), t]));

  return (
    <section>
      <header className="page-head">
        <div>
          <h2>Upstream servers</h2>
          <p className="page-sub">MCP servers the gateway discovers tools from and routes calls to.</p>
        </div>
        <RegisterForm onCreated={async (slug) => { await act(async () => undefined, `Registered ${slug}.`); }} />
      </header>
      {flash && <Notice kind="success">{flash}</Notice>}
      {actionError && <ErrorBanner error={actionError} />}
      {ups.error ? <ErrorBanner error={ups.error} onRetry={ups.reload} /> : !ups.data ? <Loading /> :
        ups.data.length === 0 ? <EmptyState>No upstreams registered.</EmptyState> :
          ups.data.map((u) => (
            <UpstreamCard key={u.slug} u={u} tools={byUpstream.get(u.slug) ?? []}
              onToggle={() => act(() => api.updateUpstream(u.slug, { enabled: !u.enabled }), `${u.slug} ${u.enabled ? "disabled" : "enabled"}.`)}
              onDiscover={() => act(() => api.discover(u.slug), `Rediscovered ${u.slug}.`)}
              onDelete={() => act(() => api.deleteUpstream(u.slug), `Removed ${u.slug}.`)}
              onSave={(patch) => act(() => api.updateUpstream(u.slug, patch), `Saved ${u.slug}.`)} />
          ))}
    </section>
  );
}

function UpstreamCard({ u, tools, onToggle, onDiscover, onDelete, onSave }: {
  u: Upstream; tools: Tool[]; onToggle: () => void; onDiscover: () => void; onDelete: () => void;
  onSave: (p: UpstreamPatch) => Promise<boolean>;
}) {
  const [editing, setEditing] = useState(false);
  return (
    <article className="card" data-testid={`upstream-card-${u.slug}`}>
      <div className="card-head">
        <div>
          <h3>{u.display_name}</h3>
          <span className="subline mono">{u.slug}</span>
        </div>
        <ReachBadge enabled={u.enabled} reachable={u.reachable} />
      </div>
      <dl className="kv">
        <dt>URL</dt><dd className="mono break">{u.url}</dd>
        <dt>Credential</dt><dd>{u.credential_ref
          ? <><span className="mono">{u.credential_ref}</span> <span className="muted small">(reference name only)</span></>
          : <span className="muted">none</span>}</dd>
        <dt>Last discovery</dt><dd><Time value={u.last_discovery_at} /></dd>
        <dt>Last success</dt><dd><Time value={u.last_success_at} /></dd>
        {u.last_error && (<><dt>Error</dt><dd className="error-text">{u.last_error}</dd></>)}
      </dl>
      <div className="row wrap">
        <button className="secondary" onClick={onToggle}>{u.enabled ? "Disable" : "Enable"}</button>
        <button className="secondary" onClick={onDiscover} disabled={!u.enabled}>Rediscover</button>
        <button className="secondary" onClick={() => setEditing((v) => !v)}>{editing ? "Close editor" : "Edit"}</button>
        <ConfirmButton label="Remove" confirmLabel={`Remove ${u.slug}`} onConfirm={onDelete} />
      </div>
      {editing && <EditForm u={u} onSave={async (p) => { if (await onSave(p)) setEditing(false); }} />}
      <details>
        <summary>{tools.length} tools{!u.enabled ? " (hidden while disabled)" : ""}</summary>
        {tools.length === 0 ? <EmptyState>No tools discovered.</EmptyState> : (
          <ul className="tools">
            {tools.map((t) => (
              <li key={t.name}>
                <span className="tool-name"><span className="mono">{t.name}</span> {t.admin_only && <Badge tone="warn">admin only</Badge>}</span>
                {t.description && <div className="muted small">{t.description}</div>}
              </li>
            ))}
          </ul>
        )}
      </details>
    </article>
  );
}

function RegisterForm({ onCreated }: { onCreated: (slug: string) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ slug: "", display_name: "", url: "", credential_ref: "", tools: "" });
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  if (!open) return <div className="actions-bar"><button onClick={() => setOpen(true)}>Register upstream</button></div>;

  async function submit(e: FormEvent) {
    e.preventDefault();
    const problem = validateUpstream(f);
    if (problem) return setError(problem);
    setBusy(true);
    setError(undefined);
    try {
      await api.createUpstream({
        slug: f.slug, display_name: f.display_name.trim(), url: f.url.trim(),
        credential_ref: f.credential_ref || null, admin_only_tools: parseTools(f.tools) as string[],
      });
      setOpen(false);
      setF({ slug: "", display_name: "", url: "", credential_ref: "", tools: "" });
      await onCreated(f.slug);
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  return (
    <form className="card form register-form" onSubmit={submit} aria-label="Register upstream">
      <h3>Register upstream</h3>
      {error && <ErrorBanner error={error} />}
      <label>Slug<input value={f.slug} onChange={set("slug")} placeholder="notes" required /></label>
      <label>Display name<input value={f.display_name} onChange={set("display_name")} required /></label>
      <label className="wide">URL<input value={f.url} onChange={set("url")} placeholder="http://name.namespace.svc.cluster.local:8000/mcp" required /></label>
      <label>Credential reference (optional)<input value={f.credential_ref} onChange={set("credential_ref")} placeholder="notes-token" /></label>
      <label className="wide">Admin-only tools (comma-separated)<input value={f.tools} onChange={set("tools")} placeholder="create_product, update_order_status" /></label>
      <p className="muted small">The gateway only accepts in-cluster URLs on approved ports; credential values live in a Kubernetes Secret.</p>
      <div className="row"><button type="submit" disabled={busy}>Register</button><button type="button" className="secondary" onClick={() => setOpen(false)}>Cancel</button></div>
    </form>
  );
}

function EditForm({ u, onSave }: { u: Upstream; onSave: (p: UpstreamPatch) => Promise<void> }) {
  const [f, setF] = useState({ display_name: u.display_name, url: u.url, credential_ref: u.credential_ref ?? "", tools: u.admin_only_tools.join(", ") });
  const [error, setError] = useState<string>();
  async function submit(e: FormEvent) {
    e.preventDefault();
    const problem = validateUpstream(f);
    if (problem) return setError(problem);
    const patch: UpstreamPatch = { display_name: f.display_name.trim(), url: f.url.trim(), admin_only_tools: parseTools(f.tools) as string[] };
    if (f.credential_ref) patch.credential_ref = f.credential_ref;
    else if (u.credential_ref) patch.clear_credential = true;
    try {
      setError(undefined);
      await onSave(patch);
    } catch (err) {
      setError((err as ApiError).message);
    }
  }
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  return (
    <form className="form edit-form" onSubmit={submit} aria-label={`Edit ${u.slug}`}>
      {error && <ErrorBanner error={error} />}
      <label>Display name<input value={f.display_name} onChange={set("display_name")} /></label>
      <label className="wide">URL<input value={f.url} onChange={set("url")} /></label>
      <label>Credential reference<input value={f.credential_ref} onChange={set("credential_ref")} placeholder="empty = none" /></label>
      <label>Admin-only tools<input value={f.tools} onChange={set("tools")} /></label>
      <button type="submit">Save</button>
    </form>
  );
}
