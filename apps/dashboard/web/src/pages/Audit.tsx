import { useState } from "react";
import { api } from "../api/client";
import type { AuditQuery, AuditRecord } from "../api/types";
import { Badge, EmptyState, ErrorBanner, Loading, Time } from "../components/ui";
import { useAsync } from "../components/useAsync";

const MAX_LIMIT = 500; // gateway maximum; there is no cursor pagination (see README "API gaps")

export function outcome(r: AuditRecord): "success" | "denied" | "failure" {
  if (r.success) return "success";
  const e = (r.error ?? "").toLowerCase();
  return e.startsWith("forbidden") || r.event === "auth.denied" ? "denied" : "failure";
}

export function Audit() {
  const [q, setQ] = useState<AuditQuery>({ limit: 50 });
  const users = useAsync(() => api.users());
  const rows = useAsync(() => api.audit(q), [JSON.stringify(q)]);
  const names = new Map((users.data ?? []).map((u) => [u.id, u.name]));
  const set = (patch: Partial<AuditQuery>) => setQ((prev) => ({ ...prev, ...patch }));

  return (
    <section>
      <header className="page-head">
        <div>
          <h2>Audit log</h2>
          <p className="page-sub">Tool calls and admin changes recorded by the gateway. Only argument names are kept; credentials are redacted.</p>
        </div>
        <button className="secondary" onClick={() => void rows.reload()}>Refresh</button>
      </header>
      <div className="card row wrap filters" role="search">
        <label>Outcome
          <select value={q.success === undefined ? "" : String(q.success)}
            onChange={(e) => set({ success: e.target.value === "" ? undefined : e.target.value === "true" })}>
            <option value="">all</option><option value="true">success</option><option value="false">failed / denied</option>
          </select>
        </label>
        <label>User
          <select value={q.user_id ?? ""} onChange={(e) => set({ user_id: e.target.value || undefined })}>
            <option value="">all</option>
            {(users.data ?? []).map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
          </select>
        </label>
        <label>Upstream<input value={q.upstream ?? ""} placeholder="slug" onChange={(e) => set({ upstream: e.target.value.trim() || undefined })} /></label>
        <label>Event
          <select value={q.event ?? ""} onChange={(e) => set({ event: e.target.value || undefined })}>
            <option value="">all</option><option value="tool_call">tool calls</option>
            <option value="admin.upstream.create">upstream created</option><option value="admin.upstream.update">upstream updated</option>
            <option value="admin.upstream.delete">upstream removed</option><option value="admin.key.create">key issued</option>
            <option value="admin.key.revoke">key revoked</option><option value="admin.user.create">user created</option>
            <option value="admin.user.update">user updated</option>
          </select>
        </label>
      </div>

      {rows.error ? <ErrorBanner error={rows.error} onRetry={rows.reload} /> : !rows.data ? <Loading /> :
        rows.data.length === 0 ? <EmptyState>No audit records match these filters.</EmptyState> : (
          <>
            <div className="table-wrap">
              <table className="audit">
                <thead><tr><th>Time</th><th>User</th><th>Event</th><th>Upstream</th><th>Tool</th><th className="num">Duration</th><th>Outcome</th><th>Error</th></tr></thead>
                <tbody>
                  {rows.data.map((r) => {
                    const o = outcome(r);
                    return (
                      <tr key={r.id} className={o} data-testid="audit-row">
                        <td className="time"><Time value={r.created_at} /></td>
                        <td className="nowrap">{r.user_id ? names.get(r.user_id) ?? <span className="mono small">{r.user_id.slice(0, 8)}</span> : <span className="muted">system</span>}</td>
                        <td><span className="event">{r.event}</span></td>
                        <td className="mono">{r.upstream_slug ?? "—"}</td>
                        <td className="mono small">{r.tool_name ?? "—"}</td>
                        <td className="num">{r.duration_ms != null ? `${r.duration_ms} ms` : "—"}</td>
                        <td>{o === "success" ? <Badge tone="ok">success</Badge> : o === "denied" ? <Badge tone="warn">denied</Badge> : <Badge tone="bad">failed</Badge>}</td>
                        <td className="small err">{r.error ?? ""}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="muted small table-foot">
              Showing the latest {rows.data.length} records.{" "}
              {rows.data.length >= (q.limit ?? 50) && (q.limit ?? 50) < MAX_LIMIT && (
                <button className="link" onClick={() => set({ limit: Math.min(MAX_LIMIT, (q.limit ?? 50) * 2) })}>Load more</button>
              )}
              {(q.limit ?? 50) >= MAX_LIMIT && "Use filters to narrow older records (the gateway returns at most 500)."}
            </p>
          </>
        )}
      {rows.loading && rows.data && <Loading label="Refreshing…" />}
    </section>
  );
}
