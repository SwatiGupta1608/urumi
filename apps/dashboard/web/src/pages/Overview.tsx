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

  const reachable = (ups.data ?? []).filter((u) => u.enabled && u.reachable).length;

  return (
    <section>
      <header className="page-head">
        <div>
          <h2>Overview</h2>
          <p className="page-sub">Gateway and upstream health. Refreshes every {REFRESH_MS / 1000}s.</p>
        </div>
        <button className="secondary" onClick={() => { void health.reload(); void tools.reload(); void ups.reload(); }}>
          Refresh
        </button>
      </header>

      <div className="card">
        <div className="card-head">
          <div>
            <h3>Gateway</h3>
            <span className="subline">MCP endpoint and admin API</span>
          </div>
          {health.data && !health.error && (health.data.gateway.ready ? <Badge tone="ok">ready</Badge> : <Badge tone="bad">not ready</Badge>)}
        </div>
        {health.error ? <ErrorBanner error={health.error} /> : !health.data ? <Loading /> :
          health.data.gateway.status !== "ready" && <p className="muted small">{health.data.gateway.status}</p>}
      </div>

      <h3 className="section-title">
        Upstream servers
        {isAdmin && ups.data && ups.data.length > 0 && (
          <span className="muted">{reachable} of {ups.data.length} reachable · {tools.data?.length ?? "…"} tools</span>
        )}
      </h3>
      {isAdmin ? (
        ups.error ? <ErrorBanner error={ups.error} onRetry={ups.reload} /> : !ups.data ? <Loading /> :
          ups.data.length === 0 ? <EmptyState>No upstreams registered yet.</EmptyState> : (
            <div className="grid">
              {ups.data.map((u) => (
                <div className="card" key={u.slug} data-testid={`upstream-${u.slug}`}>
                  <div className="card-head">
                    <div>
                      <h4>{u.display_name}</h4>
                      <span className="subline mono">{u.slug}</span>
                    </div>
                    <ReachBadge enabled={u.enabled} reachable={u.reachable} />
                  </div>
                  <div><span className="stat">{u.tool_count}</span> <span className="stat-label">tools</span></div>
                  {u.last_error && <p className="inline-error">Error: {u.last_error}</p>}
                  <p className="meta">Last successful discovery: <Time value={u.last_success_at} /></p>
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
                    <p className="stat-label"><span className="stat">{n}</span> tools available</p>
                  </div>
                ))}
              </div>
            )}
        </>
      )}
    </section>
  );
}
