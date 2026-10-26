import { vi } from "vitest";

export type Handler = (init: RequestInit, url: URL) => { status: number; body?: unknown } | Promise<{ status: number; body?: unknown }>;

/** Minimal fetch stub: route on "METHOD /path". Records calls for assertions. */
export function fakeBackend(routes: Record<string, Handler>) {
  const calls: { method: string; path: string; body?: unknown; headers: Record<string, string> }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), "http://dash.test");
    const method = (init.method ?? "GET").toUpperCase();
    const key = `${method} ${url.pathname}`;
    const headers = Object.fromEntries(Object.entries((init.headers ?? {}) as Record<string, string>));
    calls.push({ method, path: url.pathname + url.search, body: init.body ? JSON.parse(String(init.body)) : undefined, headers });
    const handler = routes[key];
    const res = handler ? await handler(init, url) : { status: 404, body: { error: "not_found", message: `no route ${key}` } };
    return new Response(res.status === 204 ? null : JSON.stringify(res.body ?? {}), {
      status: res.status, headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

export const ADMIN = { id: "a1", name: "swati", role: "admin", disabled: false, created_at: "2026-10-24T08:30:00Z" };
export const MEMBER = { id: "m1", name: "alice", role: "member", disabled: false, created_at: "2026-10-24T08:30:00Z" };
export const KEY = "urumi_0123456789ab_" + "A".repeat(43);

export const UPSTREAMS = [
  { slug: "woocommerce", display_name: "WooCommerce", url: "http://shop-woocommerce-mcp.woocommerce.svc.cluster.local:8000/mcp",
    credential_ref: null, enabled: true, admin_only_tools: ["update_order_status"], reachable: true,
    last_discovery_at: "2026-10-24T09:00:00Z", last_success_at: "2026-10-24T09:00:00Z", last_error: null, tool_count: 2,
    created_at: "2026-10-24T08:00:00Z", updated_at: "2026-10-24T08:00:00Z" },
  { slug: "github", display_name: "GitHub read-only", url: "http://gateway-github.urumi-gateway.svc.cluster.local:8082/",
    credential_ref: "github-token", enabled: true, admin_only_tools: [], reachable: false,
    last_discovery_at: "2026-10-24T09:00:00Z", last_success_at: null, last_error: "config: credential reference 'github-token' is not available",
    tool_count: 0, created_at: "2026-10-24T08:00:00Z", updated_at: "2026-10-24T08:00:00Z" },
];
export const TOOLS = [
  { name: "woocommerce__list_products", upstream: "woocommerce", title: "List products", description: null, admin_only: false },
  { name: "woocommerce__update_order_status", upstream: "woocommerce", title: null, description: null, admin_only: true },
  { name: "notes__create_note", upstream: "notes", title: null, description: null, admin_only: false },
];
export const HEALTH = { dashboard: "ok", gateway: { ready: true, status: "ready" } };
