import { api } from "../api/client";
import type { Tool, Upstream } from "../api/types";
import { useUser } from "../auth/AuthContext";
import { Badge, EmptyState, ErrorBanner, Loading, Notice, ReachBadge, Time } from "../components/ui";
import { useAsync } from "../components/useAsync";

const REFRESH_MS = 15000;

export function Overview() {
  const user = useUser();
  const isAdmin = user.role === "admin";
  const health = useAsync(() => api.health(), [], REFRESH_MS);
  const tools = useAsync(() => api.tools(), [], REFRESH_MS);
  // Upstream health is an admin-only API; members see tool counts instead.
  const ups = useAsync<Upstream[] | null>(() => (isAdmin ? api.upstreams() : Promise.resolve(null)), [isAdmin], REFRESH_MS);

  const counts = new Map<string, number>();
  (tools.data ?? []).forEach((t: Tool) => counts.set(t.upstream, (counts.get(t.upstream) ?? 0) + 1));

  return (
    <section>
      <header className="page-head">
        <h2>Overview</h2>
        <button className="secondary" onClick={() => { void health.reload(); void tools.reload(); void ups.reload(); }}>
          Refresh
        </button>
      </header>
      <p className="muted small">Refreshes every {REFRESH_MS / 1000}s.</p>

      <div className="card">
        <h3>Gateway</h3>
        {health.error ? <ErrorBanner error={health.error} /> : !health.data ? <Loading /> : (
          <p>
            {health.data.gateway.ready ? <Badge tone="ok">ready</Badge> : <Badge tone="bad">not ready</Badge>}
            {health.data.gateway.status !== "ready" && <> <span className="muted">{health.data.gateway.status}</span></>}
          </p>
        )}
      </div>

      <h3>Upstream servers</h3>
      {isAdmin ? (
        ups.error ? <ErrorBanner error={ups.error} onRetry={ups.reload} /> : !ups.data ? <Loading /> :
          ups.data.length === 0 ? <EmptyState>No upstreams registered yet.</EmptyState> : (
            <div className="grid">
              {ups.data.map((u) => (
                <div className="card" key={u.slug} data-testid={`upstream-${u.slug}`}>
                  <h4>{u.display_name} <span className="muted mono">{u.slug}</span></h4>
                  <p><ReachBadge enabled={u.enabled} reachable={u.reachable} /> {u.tool_count} tools</p>
                  <p className="small">Last successful discovery: <Time value={u.last_success_at} /></p>
                  {u.last_error && <p className="small error-text">Error: {u.last_error}</p>}
                </div>
              ))}
            </div>
          )
      ) : (
        <>
          <Notice>Upstream health details are visible to admins. You can see which servers expose tools to you.</Notice>
          {tools.error ? <ErrorBanner error={tools.error} onRetry={tools.reload} /> : !tools.data ? <Loading /> :
            counts.size === 0 ? <EmptyState>No tools are available to you right now.</EmptyState> : (
              <div className="grid">
                {[...counts.entries()].sort().map(([slug, n]) => (
                  <div className="card" key={slug} data-testid={`upstream-${slug}`}>
                    <h4 className="mono">{slug}</h4>
                    <p>{n} tools available</p>
                  </div>
                ))}
              </div>
            )}
        </>
      )}
    </section>
  );
}
