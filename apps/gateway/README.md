# Urumi MCP Gateway

One authenticated MCP endpoint (Streamable HTTP, `POST /mcp`) that aggregates
the team's in-cluster MCP servers, plus a REST API (`/api/v1`) for members and
admins. FastAPI + official MCP Python SDK (`mcp<2`), PostgreSQL for state.
Deployed by `deployments/helm/gateway` (internal ClusterIP only).

```
 member/admin ──Bearer urumi_…──▶ gateway /mcp ──(server-side credential only)──▶ woocommerce MCP
                                     │  │                                      ├─▶ notes MCP
                                     │  └─ PostgreSQL: users, key hashes,      └─▶ GitHub MCP (→ api.github.com)
                                     │     upstreams, audit
                                     └─ /api/v1 (admin: users, keys, upstreams, audit)
```

## How it works

* **MCP client + server.** At discovery and per tool call the gateway opens a
  short-lived Streamable HTTP session to the upstream (initialize → request →
  close). Upstreams are stateless MCP servers, so there is no session state to
  keep consistent, failures stay isolated per call, and shutdown has nothing to
  drain. The trade-off is one `initialize` round-trip per call, which is a few
  milliseconds in-cluster.
* **Namespacing.** Upstream tool `t` of server `s` is exposed as `s__t`. Slugs
  match `^[a-z][a-z0-9]*(-[a-z0-9]+)*$`, so they cannot contain `_` and the first `__`
  always splits slug and tool. Duplicate slugs are rejected (409). Duplicate,
  invalid (`[A-Za-z0-9_.-]{1,128}`) or over-long tool names are skipped and
  reported in the upstream's `last_error`. A tool is never overwritten by
  another upstream's tool.
* **Discovery.** Discovery runs on registration and on every edit, every
  `GATEWAY_DISCOVERY_INTERVAL` (60s), and on `POST
  /upstreams/{slug}/discover`. All enabled upstreams are discovered
  concurrently, each with its own timeout, so a slow or down upstream only
  marks itself `reachable=false` with a sanitised `last_error`. Its last good
  tool list stays registered; calls to it fail fast with a clear error.
* **Disable/remove.** Disabling or removing an upstream drops its tools
  immediately. Every call also re-reads the upstream from PostgreSQL: if it is
  disabled, deleted, or re-created with the same slug, the call is refused and
  the stale registration is dropped. This also holds across replicas.
* **Admin-only tools.** `admin_only_tools` per upstream hides those tools from
  members and refuses member calls. It is used for state-changing tools such
  as `update_order_status`. Tool annotations from upstreams (`readOnlyHint`,
  `destructiveHint`) are passed through to clients.
* **Timeouts.** Connect 3s, discovery 5s, call 30s, each with an outer bound.
  At most 16 concurrent calls per upstream.

## Authentication, roles, keys

* Keys look like `urumi_<12 hex id>_<43-char secret>`. The secret has 256 bits
  from `secrets`. Only `HMAC-SHA256(pepper, key)` is stored, and it is
  compared with `hmac.compare_digest`. The pepper is a Kubernetes Secret, so a
  database dump alone cannot be used to test guesses. The raw key is returned
  exactly once, in the create response or the CLI output.
* Every request (`/mcp` and `/api/v1`) is authenticated against PostgreSQL. A
  revoked key, or a disabled or demoted user, takes effect on the next
  request; nothing is cached.
* `member`: `GET /api/v1/me`, `GET /api/v1/me/keys` (metadata only),
  `GET /api/v1/tools`, and MCP tools except admin-only ones.
  `admin`: everything, including users, keys, upstreams and audit. The last
  active admin cannot be demoted or disabled.
* The member's key is **never forwarded**. Each upstream request uses a fresh
  HTTP client that carries only the upstream's server-side credential
  (`Authorization: Bearer <file content>`). This is covered by a test that
  echoes the headers an upstream receives.

### Bootstrap the first admin

There is no default admin, no default password and no unauthenticated
endpoint. With cluster access:

```sh
kubectl -n urumi-gateway exec -it deploy/gateway -- python -m gateway.cli create-admin --name <you>
```

This prints the key once to your terminal; the pod logs do not contain it.
The command refuses to run if an active admin already exists. Break-glass,
for an admin who lost their key:
`python -m gateway.cli issue-key --name <user>`. Both are audited.

## Upstream registration (admin)

```http
POST   /api/v1/upstreams              {slug, display_name, url, credential_ref?, enabled?, admin_only_tools?}
PATCH  /api/v1/upstreams/{slug}       {display_name?, url?, credential_ref?, clear_credential?, enabled?, admin_only_tools?}
DELETE /api/v1/upstreams/{slug}
POST   /api/v1/upstreams/{slug}/discover
GET    /api/v1/upstreams[/{slug}]     health: reachable, last_discovery_at, last_success_at, last_error, tool_count
```

`credential_ref` names a key in the Secret `gateway-upstream-credentials`,
mounted at `/etc/gateway/credentials`. The value is never stored in
PostgreSQL or returned by the API. It is re-read on every call, so rotating
the Secret needs no restart.

**SSRF rules.** A URL must be `http(s)://host:port/path` with no
credentials, query or fragment. The port must be allowed (8000, 8082). The
host must match `*.svc.cluster.local`, and every resolved address must fall
in the cluster CIDRs (10.42/16, 10.43/16) and must not be loopback,
link-local or metadata (169.254.169.254), multicast or reserved. This is
checked at registration and again before every connection. Redirects to
other origins are not followed. The gateway's NetworkPolicy egress
allow-list is the hard boundary behind all of this.

## Audit

Every MCP tool call is recorded with these fields:

* `user_id`, always from the authenticated key and never from client input,
* `upstream_slug`, `tool_name`, `created_at`, `duration_ms`, `success`,
* a sanitised `error` for denials, unknown tools, timeouts, upstream errors
  and tool errors,
* the **argument names only**. Argument values and results are not stored.

Admin actions are recorded as `admin.*` events. Error strings are passed
through a redactor before storage and logging; it masks bearer tokens,
`urumi_` keys, GitHub tokens, WooCommerce keys, URL credentials and
`token=`/`password=` pairs. Read the audit log with
`GET /api/v1/audit?user_id=&upstream=&success=&event=&limit=` (admin only).

## Configuration (env)

| Variable | Notes |
|----------|-------|
| `GATEWAY_DB_HOST/PORT/NAME/USER`, `GATEWAY_DB_PASSWORD` (Secret) | or a full `GATEWAY_DATABASE_URL` |
| `GATEWAY_KEY_PEPPER` (Secret) | ≥ 32 chars; rotating it invalidates all keys |
| `GATEWAY_CREDENTIALS_DIR` | default `/etc/gateway/credentials` |
| `GATEWAY_UPSTREAM_ALLOWED_HOSTS/CIDRS/PORTS`, `GATEWAY_UPSTREAM_ALLOW_HTTP` | SSRF policy |
| `GATEWAY_DISCOVERY_TIMEOUT`, `GATEWAY_CALL_TIMEOUT`, `GATEWAY_CONNECT_TIMEOUT`, `GATEWAY_DISCOVERY_INTERVAL` | seconds |
| `MCP_ALLOWED_HOSTS`, `MCP_ALLOWED_ORIGINS` | DNS-rebinding allow-lists for `/mcp` |

The schema is created at startup (`CREATE TABLE IF NOT EXISTS`). There is no
migration tool yet, so add Alembic before changing tables.

## Develop

```sh
uv sync && uv run pytest -q     # SQLite + mocked/in-process upstreams; no cluster, no real orders
```
