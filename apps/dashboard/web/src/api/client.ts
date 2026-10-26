// The single place the browser talks to the backend. Requests go to the
// dashboard BFF (same origin) which attaches the user's gateway key
// server-side. The browser never sees or stores an API key except the one
// returned once by key creation, which callers must keep in memory only.

import type {
  ApiKey, AuditQuery, AuditRecord, Health, NewApiKey, Role, Tool, Upstream, UpstreamInput, UpstreamPatch, User,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }

  /** The session is gone: not signed in, expired, or the key was revoked. */
  get sessionEnded(): boolean {
    return this.status === 401;
  }
}

type Listener = (err: ApiError) => void;
const sessionListeners = new Set<Listener>();

/** Subscribe to "session ended" (any 401). Returns an unsubscribe function. */
export function onSessionEnded(fn: Listener): () => void {
  sessionListeners.add(fn);
  return () => sessionListeners.delete(fn);
}

function messageFrom(status: number, body: unknown): { code: string; message: string } {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    if (typeof b.message === "string") return { code: String(b.error ?? status), message: b.message };
    // FastAPI errors: {"detail": "..."} or {"detail": [{loc, msg}, ...]}
    if (typeof b.detail === "string") return { code: String(status), message: b.detail };
    if (Array.isArray(b.detail)) {
      const msgs = b.detail.map((d: { loc?: unknown[]; msg?: string }) => {
        const field = Array.isArray(d.loc) ? d.loc.filter((x) => x !== "body").join(".") : "";
        return field ? `${field}: ${d.msg}` : String(d.msg);
      });
      return { code: "validation_error", message: msgs.join("; ") };
    }
  }
  return { code: String(status), message: `Request failed (HTTP ${status}).` };
}

async function request<T>(method: string, path: string, body?: unknown, query?: Record<string, string>): Promise<T> {
  const url = query && Object.keys(query).length ? `${path}?${new URLSearchParams(query)}` : path;
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        // Required by the BFF on writes (CSRF); harmless on reads.
        "X-Urumi-Dashboard": "1",
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "network", "Cannot reach the dashboard server. Check your connection or port-forward.");
  }
  if (res.status === 204) return undefined as T;
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    // non-JSON
  }
  if (!res.ok) {
    const { code, message } = messageFrom(res.status, data);
    const err = new ApiError(res.status, code, message);
    if (err.sessionEnded) sessionListeners.forEach((fn) => fn(err));
    throw err;
  }
  return data as T;
}

const enc = encodeURIComponent;

export const api = {
  // session (BFF)
  login: (apiKey: string) => request<{ user: User }>("POST", "/bff/login", { api_key: apiKey }),
  logout: () => request<{ ok: boolean }>("POST", "/bff/logout"),
  session: () => request<{ user: User }>("GET", "/bff/session"),
  health: () => request<Health>("GET", "/bff/health"),

  // member
  me: () => request<User>("GET", "/api/v1/me"),
  myKeys: () => request<ApiKey[]>("GET", "/api/v1/me/keys"),
  tools: () => request<Tool[]>("GET", "/api/v1/tools"),

  // admin: users & keys
  users: () => request<User[]>("GET", "/api/v1/users"),
  createUser: (name: string, role: Role) => request<User>("POST", "/api/v1/users", { name, role }),
  updateUser: (id: string, patch: { role?: Role; disabled?: boolean }) =>
    request<User>("PATCH", `/api/v1/users/${enc(id)}`, patch),
  userKeys: (id: string) => request<ApiKey[]>("GET", `/api/v1/users/${enc(id)}/keys`),
  createKey: (id: string, name: string) => request<NewApiKey>("POST", `/api/v1/users/${enc(id)}/keys`, { name }),
  revokeKey: (keyId: string) => request<void>("DELETE", `/api/v1/keys/${enc(keyId)}`),

  // admin: upstreams
  upstreams: () => request<Upstream[]>("GET", "/api/v1/upstreams"),
  createUpstream: (body: UpstreamInput) => request<Upstream>("POST", "/api/v1/upstreams", body),
  updateUpstream: (slug: string, patch: UpstreamPatch) => request<Upstream>("PATCH", `/api/v1/upstreams/${enc(slug)}`, patch),
  deleteUpstream: (slug: string) => request<void>("DELETE", `/api/v1/upstreams/${enc(slug)}`),
  discover: (slug: string) => request<Upstream>("POST", `/api/v1/upstreams/${enc(slug)}/discover`),

  // admin: audit
  audit: (q: AuditQuery) => {
    const query: Record<string, string> = {};
    if (q.limit) query.limit = String(q.limit);
    if (q.user_id) query.user_id = q.user_id;
    if (q.upstream) query.upstream = q.upstream;
    if (q.success !== undefined) query.success = String(q.success);
    if (q.event) query.event = q.event;
    return request<AuditRecord[]>("GET", "/api/v1/audit", undefined, query);
  },
};

export type Api = typeof api;
