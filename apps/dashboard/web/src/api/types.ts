// Mirrors the gateway's response models (apps/gateway/gateway/api.py). Keep in sync.

export type Role = "admin" | "member";

export interface User {
  id: string;
  name: string;
  role: Role;
  disabled: boolean;
  created_at: string;
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  revoked_at: string | null;
  last_used_at: string | null;
}

/** Returned once by POST /users/{id}/keys. `key` must never be persisted. */
export interface NewApiKey extends ApiKey {
  key: string;
}

export interface Upstream {
  slug: string;
  display_name: string;
  url: string;
  credential_ref: string | null;
  enabled: boolean;
  admin_only_tools: string[];
  reachable: boolean | null;
  last_discovery_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  tool_count: number;
  created_at: string;
  updated_at: string;
}

export interface UpstreamInput {
  slug: string;
  display_name: string;
  url: string;
  credential_ref?: string | null;
  enabled?: boolean;
  admin_only_tools?: string[];
}

export interface UpstreamPatch {
  display_name?: string;
  url?: string;
  credential_ref?: string;
  clear_credential?: boolean;
  enabled?: boolean;
  admin_only_tools?: string[];
}

export interface Tool {
  name: string; // namespaced: <slug>__<tool>
  upstream: string;
  title: string | null;
  description: string | null;
  admin_only: boolean;
}

export interface AuditRecord {
  id: string;
  created_at: string;
  event: string;
  user_id: string | null;
  upstream_slug: string | null;
  tool_name: string | null;
  success: boolean;
  duration_ms: number | null;
  error: string | null;
  detail: Record<string, unknown> | null;
}

export interface AuditQuery {
  limit?: number;
  user_id?: string;
  upstream?: string;
  success?: boolean;
  event?: string;
}

export interface Health {
  dashboard: string;
  gateway: { ready: boolean; status: string };
}
