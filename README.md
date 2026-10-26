# Urumi MCP Gateway on Kubernetes

A self-hosted **MCP gateway** that lets a team use several MCP servers
(WooCommerce, team Notes, GitHub) through **one authenticated endpoint**. Every
user has their own API key, admins control which servers and tools exist, and
every tool call is attributed and audited. A web dashboard manages it, and a
server-side chat agent can drive the tools with explicit confirmation for
anything that changes data. Everything runs on Kubernetes (k3d locally, with
values-only changes intended for k3s).

> **Status (commit `0aa22dd`):** the gateway, all three upstream MCP servers,
> the dashboard and the browser end-to-end flow are deployed and verified on a
> local k3d cluster. **The chat agent has not been run against a real LLM
> provider.** It is implemented and covered by mocked tests, but no provider
> was configured, so the deployed chat reports "unavailable" (see
> [Known limitations](#known-limitations)).

---

## Contents

1. [Architecture](#architecture)
2. [Components](#components)
3. [Prerequisites](#prerequisites)
4. [Local setup (k3d)](#local-setup-k3d)
5. [Secrets](#secrets)
6. [WooCommerce and its MCP tools](#woocommerce-and-its-mcp-tools)
7. [Gateway: auth, keys, discovery, authorization](#gateway-auth-keys-discovery-authorization)
8. [Dashboard](#dashboard)
9. [Connecting an external MCP client](#connecting-an-external-mcp-client)
10. [Demo script](#demo-script)
11. [Tests and verified results](#tests-and-verified-results)
12. [Known limitations](#known-limitations)
13. [Security assumptions and threat model](#security-assumptions-and-threat-model)
14. [Teardown](#teardown)
15. [Repository layout](#repository-layout)

---

## Architecture

```
                     your laptop (kubectl port-forward only; nothing below has a public Ingress
                     except the WooCommerce storefront)
                               │                         │
             browser ──────────┤                         ├──────── MCP client (e.g. MCP Inspector)
                               ▼                         ▼
┌──────────────────────── namespace: urumi-gateway ─────────────────────────────────────────────┐
│  gateway-dashboard :8090                          gateway :8080                               │
│  React SPA + Python BFF                           FastAPI + MCP SDK (<2)                      │
│  • HttpOnly session ↔ user's key (memory)  ──►   • POST /mcp  (Streamable HTTP, Bearer key)  │
│  • proxies /api/v1 (allow-list)                    • /api/v1  (users, keys, upstreams, audit) │
│  • chat agent → LLM (only if configured)          • per-user tools, admin-only tools, audit   │
│                                                     │            │             │             │
│                         gateway-postgres :5432 ◄────┘            │             │             │
│                         users, key hashes,                       ▼             ▼             │
│                         upstreams, audit             gateway-notes :8000   gateway-github :8082
│                                                      Notes MCP (SQLite)    github-mcp-server  │
│                                                                            --read-only ──► api.github.com
└───────────────────────────────────────────────────────────────┬──────────────────────────────┘
                                                                │ MCP (labelled pods only)
┌──────────────────────── namespace: woocommerce ───────────────▼──────────────────────────────┐
│  shop-woocommerce-mcp :8000  ──HTTPS (dev CA, SNI shop.localtest.me)──► Traefik ──► WordPress │
│  5 tools, server-side WooCommerce REST key                                   + WooCommerce    │
│                                                                    shop-woocommerce-mariadb   │
└───────────────────────────────────────────────────────────────────────────────────────────────┘
        Storefront for people: https://shop.localtest.me:8443  (Traefik Ingress; k3d maps 8443→443)
```

Every arrow above is also an explicit **NetworkPolicy** rule. Pods can only
reach what is drawn, plus cluster DNS.

## Components

| Component | Path | Responsibility |
|-----------|------|----------------|
| WooCommerce chart | `deployments/helm/woocommerce` | MariaDB, WordPress 7.1.3 (PHP 8.3.36, pinned by digest), Traefik Ingress with a local dev-CA TLS certificate, an init Job that installs WooCommerce 11.2.1 and a least-privilege REST key, and the WooCommerce MCP server |
| WooCommerce MCP | `apps/woocommerce-mcp` | 5 validated tools over Streamable HTTP. Calls WooCommerce through Traefik over verified TLS with a server-side key |
| Gateway | `apps/gateway` | MCP server *and* MCP client. Handles API-key auth, roles, namespaced tool aggregation, admin-only tools, SSRF-checked upstream registry, audit log |
| Notes MCP | `apps/notes-mcp` | Shared team notes (SQLite on a PVC). Requires a bearer token that only the gateway holds |
| GitHub MCP | `ghcr.io/github/github-mcp-server:v2.0.2` | Official server in HTTP, `--read-only` mode. The GitHub token is the gateway's credential for it |
| Dashboard | `apps/dashboard` | React + TypeScript SPA served by a Python BFF that keeps the user's key server-side; the chat agent runs here |
| Gateway chart | `deployments/helm/gateway` | Gateway, PostgreSQL 17.11, Notes, GitHub MCP, dashboard, and their NetworkPolicies (`urumi-gateway` namespace) |
| Spike | `spikes/hello-mcp` | The original two-tool MCP proof of concept (unchanged) |

Per-component details are in each component's README (linked under
[Repository layout](#repository-layout)).

## Prerequisites

Verified with these versions on macOS (arm64). Similar versions should work.

| Tool | Version used |
|------|--------------|
| Docker (Desktop) | 29.7.2 |
| k3d | v5.9.0 (k3s v1.35.5) |
| kubectl | v1.37.1 |
| Helm | v4.3.0 |
| uv (Python 3.12 is fetched by uv) | 0.13.0 |
| Node.js / npm | 24.21 / 11.19 |
| openssl (LibreSSL works) | LibreSSL 3.3.6 |

The machine also needs internet access: images, PyPI and npm packages, the
WooCommerce plugin download, and the GitHub API.

## Local setup (k3d)

All commands run from the repository root.

### 1. Cluster

The project uses a k3d cluster named `urumi` with one server, one agent, and
the load balancer mapping host **8080→80** and **8443→443**. If you don't
have it yet:

```sh
k3d cluster create urumi --agents 1 -p "8080:80@loadbalancer" -p "8443:443@loadbalancer"
kubectl get nodes          # both Ready
```

(The cluster used for verification was created earlier with this layout.
This command reproduces its server/agent count and port mapping.)

### 2. WooCommerce store and its MCP server

```sh
deployments/helm/woocommerce/scripts/create-secrets.sh woocommerce     # random DB/WP/API-key Secrets, never printed
deployments/helm/woocommerce/scripts/create-local-tls.sh woocommerce   # dev CA + cert for shop.localtest.me (CA key discarded)
docker build -t urumi/woocommerce-mcp:0.1.0 apps/woocommerce-mcp
k3d image import urumi/woocommerce-mcp:0.1.0 -c urumi
helm lint deployments/helm/woocommerce
helm upgrade --install shop deployments/helm/woocommerce -n woocommerce --wait --timeout 15m
kubectl -n woocommerce logs job/shop-woocommerce-init -c wp-cli      # "[init] done"
```

Optionally, seed three **synthetic** demo orders (fake `example.invalid`
customers, no payments), then return the release to chart defaults:

```sh
helm upgrade shop deployments/helm/woocommerce -n woocommerce --set init.demoData.enabled=true --wait
helm upgrade shop deployments/helm/woocommerce -n woocommerce --reset-values --wait
kubectl -n woocommerce logs job/shop-woocommerce-init -c wp-cli | grep 'demo orders'   # e.g. status-test order id
```

> This Helm 4 build reuses the previous release's `--set` values on a plain
> `helm upgrade`. That's why `--reset-values` is used to switch seeding
> back off.

The storefront is at <https://shop.localtest.me:8443/>. It uses the dev CA,
so browsers warn; for curl, use `--cacert` (see the WooCommerce chart
README).

### 3. Gateway, upstream servers and dashboard

```sh
deployments/helm/gateway/scripts/create-secrets.sh urumi-gateway      # key pepper, Postgres password, notes token

# Build the local images. If downloads inside Docker are slow, prefetch the locked
# wheels on the host (hash-checked against uv.lock); otherwise drop --build-context.
scripts/prefetch-wheels.sh apps/gateway && scripts/prefetch-wheels.sh apps/notes-mcp && scripts/prefetch-wheels.sh apps/dashboard
docker build --build-context wheels=apps/gateway/.wheels   -t urumi/gateway:0.1.0   apps/gateway
docker build --build-context wheels=apps/notes-mcp/.wheels -t urumi/notes-mcp:0.1.0 apps/notes-mcp
(cd apps/dashboard/web && npm ci && npm run build)          # static SPA, copied into the image
docker build --build-context wheels=apps/dashboard/.wheels -t urumi/dashboard:0.2.2 apps/dashboard
k3d image import urumi/gateway:0.1.0 urumi/notes-mcp:0.1.0 urumi/dashboard:0.2.2 -c urumi

helm lint deployments/helm/gateway
helm template gateway deployments/helm/gateway -n urumi-gateway | less    # review before deploying
helm upgrade --install gateway deployments/helm/gateway -n urumi-gateway --wait --timeout 10m
```

The WooCommerce chart's default values already admit the gateway's namespace
to the WooCommerce MCP server, so no extra step is needed for that.

### 4. First admin (bootstrap)

There is no default admin, password or unauthenticated admin endpoint.
Anyone with cluster access creates the first admin. The key is printed
**once**, to your terminal only:

```sh
kubectl -n urumi-gateway exec -it deploy/gateway -- python -m gateway.cli create-admin --name <you>
```

Store the printed `api_key` in a password manager. The command refuses to run
if an active admin already exists. If an admin loses their key, the
break-glass command is
`python -m gateway.cli issue-key --name <user>`. Both actions are audited.

### 5. Register the upstreams (admin API)

Read your key into a shell variable without echoing it:

```sh
read -rs ADMIN_KEY            # paste the key, press Enter (input hidden)
kubectl -n urumi-gateway port-forward svc/gateway 18080:8080 &
H="Authorization: Bearer $ADMIN_KEY"
curl -s -H "$H" -H 'Content-Type: application/json' localhost:18080/api/v1/upstreams -d '{"slug":"woocommerce","display_name":"WooCommerce","url":"http://shop-woocommerce-mcp.woocommerce.svc.cluster.local:8000/mcp","admin_only_tools":["create_product","update_order_status"]}'
curl -s -H "$H" -H 'Content-Type: application/json' localhost:18080/api/v1/upstreams -d '{"slug":"notes","display_name":"Team notes","url":"http://gateway-notes.urumi-gateway.svc.cluster.local:8000/mcp","credential_ref":"notes-token"}'
curl -s -H "$H" -H 'Content-Type: application/json' localhost:18080/api/v1/upstreams -d '{"slug":"github","display_name":"GitHub read-only","url":"http://gateway-github.urumi-gateway.svc.cluster.local:8082/","credential_ref":"github-token"}'
curl -s -H "$H" localhost:18080/api/v1/upstreams | python3 -m json.tool | grep -E '"slug"|"reachable"|"tool_count"|"last_error"'
```

GitHub stays `reachable: false` with the error "credential reference
'github-token' is not available" until you add a token
([Secrets](#secrets)). The other upstreams are unaffected.

## Secrets

**No credential is in this repository, in Helm values, or in API
responses.** Every Secret is created out-of-band by a script that generates
random values, writes them to a private temp directory, loads them with
`--from-file`, and never prints them.

| Secret (namespace) | Keys | Created by |
|--------------------|------|------------|
| `woocommerce-db` (woocommerce) | `mariadb-root-password`, `mariadb-password` | `deployments/helm/woocommerce/scripts/create-secrets.sh` |
| `woocommerce-wordpress` (woocommerce) | `admin-password`, 8 WordPress salts | same |
| `woocommerce-api-key` (woocommerce) | `consumer-key`, `consumer-secret` | same |
| `shop-tls`, `shop-tls-ca` (woocommerce) | TLS cert/key, dev CA certificate | `deployments/helm/woocommerce/scripts/create-local-tls.sh` |
| `gateway-secrets` (urumi-gateway) | `key-pepper` (HMAC pepper for API-key hashes) | `deployments/helm/gateway/scripts/create-secrets.sh` |
| `gateway-postgres` (urumi-gateway) | `password` | same |
| `gateway-upstream-credentials` (urumi-gateway) | `notes-token`, optional `github-token` | same; `github-token` via `set-github-token` |
| `dashboard-llm` (urumi-gateway, optional) | `api-key` (LLM provider key) | you (see [Dashboard](#chat-agent)) |

**GitHub token.** Use a fine-grained token with *public repositories,
read-only* access. Either type it at the hidden prompt in your own terminal,
or pipe it from the clipboard so it never appears on screen or in argv:

```sh
deployments/helm/gateway/scripts/create-secrets.sh set-github-token urumi-gateway          # hidden prompt
pbpaste | deployments/helm/gateway/scripts/create-secrets.sh set-github-token urumi-gateway # macOS clipboard
```

The gateway re-reads credential files on every call, so no restart is
needed. Don't paste tokens or keys into chats, tickets or shell history. If
one leaks, revoke it and store a new one.

Rotation: delete a Secret and re-run its script (`create-local-tls.sh
--rotate` for TLS). **Rotating `key-pepper` invalidates every gateway API
key.**

## WooCommerce and its MCP tools

The init Job installs WordPress, WooCommerce 11.2.1 (pinned), disables
"coming soon" mode, and creates a REST key bound to a dedicated user. That
user's custom role (`urumi_product_api`) has only product and order
capabilities. It has no access to customers, coupons, settings or refunds;
those return 403.

The WooCommerce MCP server (`shop-woocommerce-mcp`, ClusterIP only) exposes
exactly five tools with strict input schemas:

| Tool | WooCommerce call | Notes |
|------|------------------|-------|
| `list_products` | `GET /products` | `page` 1–1000, `per_page` 1–50, optional `search` |
| `create_product` | `POST /products` | simple product; validated name/price/description; **admin-only in the gateway** |
| `list_orders` | `GET /orders` | optional status filter |
| `get_order` | `GET /orders/{id}` | returns billing name and email (personal data) |
| `update_order_status` | `GET` + `PUT /orders/{id}` | allowed statuses only, never `refunded`; may send emails or adjust stock; **admin-only in the gateway** |

WooCommerce only accepts consumer-key Basic auth over HTTPS. The MCP server
therefore calls Traefik's in-cluster Service over TLS. It sends SNI and Host
`shop.localtest.me` and verifies the certificate against the dev CA, trusted
by that client only. See `apps/woocommerce-mcp/README.md`.

## Gateway: auth, keys, discovery, authorization

* **Endpoints:**
  * `POST /mcp`: Streamable HTTP MCP, stateless JSON responses.
  * `/api/v1`: REST.
  * `/healthz`: liveness.
  * `/readyz`: checks the database.
* **Authentication:** `Authorization: Bearer urumi_<12-hex id>_<43-char secret>`
  on every request. The secret has 256 bits of randomness. Only
  `HMAC-SHA256(pepper, key)` is stored, and it is compared in constant time.
  Keys are checked against PostgreSQL on every request (no cache), so a
  **revoked key, or a disabled or demoted user, is rejected on the very
  next request**. A new key is shown exactly once.
* **Roles:**
  * `member`: their own profile and key metadata (`/me`, `/me/keys`),
    `/tools`, and MCP tools except admin-only ones.
  * `admin`: also users, keys, upstreams and audit. The last active admin
    cannot be demoted or disabled.
* **REST routes:**
  * `GET /api/v1/me`, `GET /me/keys`, `GET /tools`
  * `GET|POST /users`, `GET|PATCH /users/{id}`, `GET|POST /users/{id}/keys`,
    `DELETE /keys/{key_id}`
  * `GET|POST /upstreams`, `GET|PATCH|DELETE /upstreams/{slug}`,
    `POST /upstreams/{slug}/discover`
  * `GET /audit?limit=&user_id=&upstream=&success=&event=`,
    `GET /admin/status`
  * There is no user delete route; disable users instead.
* **Discovery and namespacing:** a tool `t` on upstream `s` is exposed as
  `s__t`.
  * Slugs cannot contain `_`, so names never collide.
  * Duplicate slugs are rejected (409).
  * Duplicate or invalid tool names are skipped and reported, never
    overwritten.
  * Discovery runs at registration, on every edit, every 60s, and on demand,
    with a separate timeout for each upstream. A slow or down upstream only
    marks itself `reachable: false` with a sanitised `last_error`.
* **Authorization at call time:** every call re-reads the upstream from the
  database, so a disabled, deleted or re-created upstream cannot be called
  through a stale registration. `admin_only_tools` are hidden from members,
  and their calls are refused.
* **Upstream credentials** are only *references* to files mounted from
  `gateway-upstream-credentials`, sent as `Bearer` to that upstream. A
  member's own gateway key is **never forwarded**.
* **SSRF policy for upstream URLs:**
  * The scheme must be `http(s)://host:port/path`, with no credentials or
    query.
  * The port must be 8000 or 8082, and the host must match
    `*.svc.cluster.local`.
  * Resolved addresses must fall in 10.42/16 or 10.43/16 and must not be
    loopback, link-local or metadata addresses.
  * This is checked at registration and again before each connection, and
    redirects to other origins are not followed. The NetworkPolicy egress
    allow-list is the hard boundary behind it.
* **Audit:** each tool call records the user (from the key, never from the
  request), upstream, tool, time, duration, success, a sanitised error, and
  **argument names only**. Admin actions are recorded as `admin.*` events.

Details: `apps/gateway/README.md`.

## Dashboard

```sh
kubectl -n urumi-gateway port-forward svc/gateway-dashboard 18090:8090
# open http://localhost:18090 and sign in with YOUR OWN gateway API key
```

* **Session security:**
  * The key is sent once, verified against the gateway (`GET /api/v1/me`),
    and kept only in the BFF's memory, behind an opaque `HttpOnly`,
    `SameSite=Strict` cookie.
  * Nothing is written to browser storage; an ESLint rule bans it.
  * Writes need an `X-Urumi-Dashboard: 1` header and a same-origin
    `Origin`.
  * The BFF forwards only the gateway's real routes.
  * Sessions end after 30 minutes idle, 8 hours total, on logout, or when
    the gateway rejects the key (revoked key or disabled user).
  * Single replica; a restart signs everyone out.
* **Admin vs member:**
  * Admins see Overview (upstream health), Upstreams (register, edit,
    enable/disable, remove, rediscover, tools with admin-only badges),
    Team & keys (users, roles, keys shown once, revoke), Audit log and Chat.
  * Members see Overview (tool counts), "My account" (their own key
    metadata) and Chat.
  * The gateway enforces all of this, so hiding pages is only a convenience.
* **No public Ingress.** `dashboard.secureCookies` is `false` for
  plain-HTTP port-forwarding; set it to `true` behind TLS.

### Chat agent

The agent runs inside the BFF:

```
browser → /bff/chat/* → BFF ─► OpenAI-compatible LLM (key from Secret)
                         └──► gateway POST /mcp with the user's own key → upstream tools
```

* **Tools:** only those the gateway lists for *that user*. Every argument is
  checked against the tool's JSON schema before any call. Limits: 6 model
  steps and 8 tool calls per turn, with tool output truncated.
* **Confirmation:** required for **every tool except a reviewed allow-list**
  (`dashboard.chat.readOnlyTools`), which contains 25 tools:
  * WooCommerce `list_products`, `list_orders`, `get_order` (only `GET`
    requests),
  * Notes `list_notes`, `get_note` (only `SELECT` queries),
  * 20 GitHub `get_`/`list_`/`search_`/`issue_read` tools (the server runs
    `--read-only`).

  Upstream "read-only" metadata never skips confirmation by itself, but
  metadata that marks a listed tool as mutating forces confirmation again.
* **Pending actions** are bound to the session and an exact digest of the
  tool and its arguments. They expire after 5 minutes and run at most once.
  On confirm, tool availability and arguments are re-checked.
* **Output safety:** tool output is labelled untrusted for the model and
  rendered as plain text in the UI.
* **Configuring a provider.** With none configured (the default), chat
  shows "unavailable" and returns `503 provider_not_configured`; nothing is
  simulated. To configure one:

  ```sh
  pbpaste | kubectl -n urumi-gateway create secret generic dashboard-llm --from-file=api-key=/dev/stdin
  helm upgrade gateway deployments/helm/gateway -n urumi-gateway --reuse-values \
    --set dashboard.chat.provider.baseUrl=https://<openai-compatible-endpoint>/v1 \
    --set dashboard.chat.provider.model=<model-with-tool-calling>
  ```

  Setting `baseUrl` also adds an HTTPS-only egress rule for the dashboard
  pod. Order results contain customer names and emails and are sent to the
  provider, so check its data-handling terms first.

Details: `apps/dashboard/README.md`.

## Connecting an external MCP client

The gateway is internal-only. Reach it with a port-forward and your own key.
The Host header must be `localhost:<port>` (DNS-rebinding protection), and
without a key the endpoint returns 401.

```sh
kubectl -n urumi-gateway port-forward svc/gateway 18080:8080
```

* **URL:** `http://localhost:18080/mcp`
* **Transport:** Streamable HTTP
* **Header:** `Authorization: Bearer <your key>`

With MCP Inspector (the version used for verification):

```sh
read -rs KEY
npx -y @modelcontextprotocol/inspector@2.10.1 --cli http://localhost:18080/mcp --transport http \
  --header "Authorization: Bearer $KEY" --method tools/list
npx -y @modelcontextprotocol/inspector@2.10.1 --cli http://localhost:18080/mcp --transport http \
  --header "Authorization: Bearer $KEY" --method tools/call --tool-name woocommerce__list_products --tool-arg per_page=3
```

For the Inspector UI, run `npx @modelcontextprotocol/inspector@2.10.1` and
choose transport "Streamable HTTP", URL `http://localhost:18080/mcp`, and an
`Authorization` header. (The CLI was verified; the UI was not.)

## Demo script

This uses the setup above. Keep the two port-forwards running
(gateway on 18080, dashboard on 18090), and read keys with `read -rs`. Use
**only the synthetic demo orders** for order operations; never a real
customer order.

| # | Step | How | Expected |
|---|------|-----|----------|
| 1 | Health | Dashboard → Overview (admin) | Gateway `ready`; WooCommerce (5), Notes (4), GitHub (20 with token) `reachable` |
| 2 | Tool discovery | `tools/list` via Inspector with the admin key | 29 tools, all named `slug__tool` |
| 3 | Product listing | `tools/call woocommerce__list_products per_page=3` | Products returned |
| 4 | Notes | `tools/call notes__create_note` with `title=Demo`, `body=Hello`, then `notes__list_notes` | Note created and listed |
| 5 | Order read | `woocommerce__list_orders`, then `woocommerce__get_order order_id=<demo id>` | Demo orders shown |
| 6 | Order write (admin) | `woocommerce__update_order_status` with `order_id=<status-test id>` and `status=on-hold`, then the same with `status=pending` | Status changes and is restored; WooCommerce may try to send an email |
| 7 | Member restrictions | Dashboard → Team & keys → create a member, issue a key (shown once), sign in as the member in a private window | No Upstreams or Audit pages; `tools/list` with the member key lacks `create_product` and `update_order_status`; calling `update_order_status` returns "requires the admin role" |
| 8 | Audit | Dashboard → Audit log (admin) | Steps 3–7 attributed to the right user, with duration and outcome; denials marked "denied"; argument names only |
| 9 | Key revocation | Admin revokes the member's key in Team & keys | The member's next click returns to login with "API key was rejected"; `/mcp` with that key returns 401 |
| 10 | Upstream disablement | Upstreams → Disable `notes`; list tools; call `notes__list_notes`; then Enable | `notes__*` tools disappear and the call is refused; they return after enabling |
| 11 | Failure isolation | `kubectl -n urumi-gateway scale deploy/gateway-notes --replicas=0`, Rediscover `notes`, call a WooCommerce tool; then `--replicas=1` | Notes shows `unreachable` with a sanitised error; WooCommerce and GitHub keep working |
| 12 | Confirmed and cancelled writes | Chat page | **Requires an LLM provider (not verified live).** Without one, Chat shows "unavailable". The confirm/cancel/expiry flow is covered by automated tests |

Steps 1–5 and 7–10 were exercised on the live cluster during development,
via dashboard, Inspector, curl and an automated browser run. These steps
were **not** run live exactly as written:
* **Step 6:** an admin changing an order's status *through the gateway*.
  The live order-status test went directly to the WooCommerce MCP server
  with `integration_check.py` (demo order `pending → on-hold → pending`).
  Members being refused this tool through the gateway *was* verified live.
* **Step 11:** scaling Notes to zero. Isolation was verified live with an
  unavailable GitHub upstream, and with slow, down and timing-out upstreams
  in automated tests.
* **Step 12:** not run against a real LLM.

`apps/woocommerce-mcp/scripts/integration_check.py` is an in-cluster check
of the WooCommerce MCP server. It creates one uniquely
named product; with `--status-test-order <id>` it moves only that demo order
`pending → on-hold → pending`. See `apps/woocommerce-mcp/README.md`.

## Tests and verified results

### Automated tests (run on commit `0aa22dd`)

```sh
(cd apps/woocommerce-mcp && uv run pytest -q)     # 92 passed
(cd apps/gateway        && uv run pytest -q)     # 92 passed
(cd apps/notes-mcp      && uv run pytest -q)     # 3 passed
(cd apps/dashboard      && uv run pytest -q)     # 80 passed (BFF + chat agent; LLM and gateway mocked)
(cd apps/dashboard/web  && npm ci && npm run typecheck && npm run lint && npm test && npm run build)   # 26 passed
helm lint deployments/helm/woocommerce && helm lint deployments/helm/gateway
```

Unit tests use mocks, SQLite and in-process MCP servers. They never touch the
real store. Coverage includes:
* key hashing and revocation, roles, and SSRF rules,
* namespacing and collisions, slow, down or malformed upstreams, and
  timeouts,
* audit attribution, no key forwarding, and credential redaction,
* the BFF session, CSRF and proxy allow-list,
* the chat tool loop, schema validation, provider errors, and mutation
  preview, confirm, cancel, expiry and session binding,
* frontend role-based rendering, key-once handling, and safe rendering.

### Live-cluster checks (performed during development)

* **WooCommerce:**
  * The storefront serves over the dev-CA TLS certificate.
  * The REST key can create and read products, and gets 403 on customers,
    coupons, settings, system status, order create/delete and other
    unrelated resources.
  * Data persists across pod restarts.
  * MariaDB is reachable only from WordPress and the init Job.
  * PHP 8.3.36 is confirmed in the running Apache SAPI.
* **Gateway:**
  * MCP Inspector (2.10.1, CLI) listed namespaced tools from all three
    upstreams and called at least one tool on each.
  * A member was denied an admin-only tool server-side.
  * Revocation worked on the next request, and disabling an upstream
    removed its tools.
  * The audit log attributed calls correctly.
  * NetworkPolicies allow only the intended paths in both directions.
  * No credentials appeared in pod logs, API responses or a PostgreSQL
    dump.
* **Dashboard:**
  * A browser end-to-end run with Playwright and Chromium against dashboard
    image `0.2.2` **passed 26/26 checks**.
  * It covered login and bad keys, cookie and storage hygiene, every page,
    one-time key display, member restrictions, revocation ending the
    session, the mobile layout, and no JavaScript errors.
  * The Playwright script was run ad hoc and is **not committed**.
* **Chat agent:**
  * The BFF's MCP client listed 29 tools through the gateway from inside the
    dashboard pod and called `notes__list_notes`, which was audited under
    the signed-in user.
  * Chat correctly reports "provider not configured".

### Not verified

* **The chat agent with a real LLM provider.** Tool selection, schema
  adherence and multi-step behaviour of a real model are untested.
* Deployment on a k3s VPS (`values-vps.example.yaml` exists but was never
  applied), and amd64 builds of the locally built images.
* The MCP Inspector *UI* (only the CLI was used), and demo step 11 exactly
  as written.

## Known limitations

* **Live LLM chat has not been tested** (see above). Without a provider,
  chat is disabled.
* **Single replicas:** the gateway's tool registry and the dashboard's
  sessions and conversations are in-process. Restarts sign users out and
  clear chat history.
* **No schema migrations:** the gateway creates its tables at startup
  (`CREATE TABLE IF NOT EXISTS`).
* **API keys never expire.** There is no rate limiting except on dashboard
  login.
* **Audit log:** no cursor pagination, only `limit` (up to 500) and
  filters.
* **Users cannot be deleted,** only disabled. Members cannot create or revoke
  their own keys.
* **Upstreams don't see member identity:** Notes are shared team-wide, and
  GitHub acts as the token owner for everyone.
* **Personal data:** order tools return customer name and email. Chat would
  send them to the LLM provider.
* **Locally built images** (`urumi/*`) are imported into k3d and not pinned
  by digest. Images pulled from registries are pinned.
* **Local TLS** uses a throwaway dev CA, and plain-HTTP port-forwarding is
  used for the gateway and dashboard.
* **Leftover local test data:** demo products and orders, and some disabled
  test users with revoked keys. Disabling is the only supported cleanup.

## Security assumptions and threat model

**Assumptions:**
* Anyone with Kubernetes cluster access (`kubectl`) is trusted at admin
  level: they can read Secrets and bootstrap admins.
* The node and the container runtime are trusted.
* Upstream MCP servers are registered by admins and trusted to implement
  their own tools honestly.

**What is defended:**

| Threat | Mitigation |
|--------|------------|
| Stolen database dump reveals keys | Only peppered HMAC hashes are stored; the pepper lives in a Secret |
| Revoked or departed user keeps access | Keys and users are checked on every request; revocation is immediate |
| Member escalates to admin actions | Server-side role checks in the gateway; admin-only tools are hidden and refused; the UI is not trusted |
| Forged audit attribution | User ID comes from the authenticated key, never from request data |
| Member key leaks to upstreams | Fresh HTTP client per upstream call carrying only the server-side credential (tested) |
| SSRF via upstream registration | Admin-only registration; host, port and CIDR allow-lists; metadata, loopback and link-local blocked; DNS re-checked; egress NetworkPolicy |
| Lateral movement inside the cluster | NetworkPolicies on every component: databases accept only their app, upstreams accept only the gateway, the gateway reaches only its upstreams |
| Key theft from the browser | Key kept in BFF memory; `HttpOnly` + `SameSite=Strict` cookie; no browser storage; strict CSP; CSRF header + Origin check |
| LLM performs unintended writes / prompt injection | Server-side tool allow-list per user, schema validation, confirmation for every non-reviewed tool, single-use bound pending actions, untrusted-output labelling |
| Credential leakage in logs and errors | Redaction of bearer tokens, keys and tokens in stored and logged errors; only argument names in audit; secret scans before each commit |
| Vulnerable PHP | WordPress image pinned by digest to PHP 8.3.36 (fixes CVE-2026-109618/109619); `session.upload_progress.enabled=0` |

**Residual risks:**
* A malicious *admin*, or a lying upstream, is out of scope.
* Prompt injection within read-only tools can still mislead a model.
* DNS-rebinding between the URL check and the connection is backstopped
  only by NetworkPolicy.
* Single-replica state.
* No TLS on the port-forwarded internal hops.

## Teardown

> ⚠️ **The commands below delete data. Read each one before running it.**

```sh
# Remove releases. PVCs are KEPT: WordPress wp-content, the Notes data, and the
# StatefulSet volumes for MariaDB and PostgreSQL. Secrets are also kept.
helm uninstall gateway -n urumi-gateway
helm uninstall shop -n woocommerce

# ⚠️ DESTRUCTIVE: deletes the namespaces with all their PVCs, Secrets and data
# (store, orders, users, API-key hashes, audit log, notes).
kubectl delete namespace urumi-gateway woocommerce

# ⚠️ DESTRUCTIVE: deletes the whole local cluster and everything in it.
k3d cluster delete urumi
```

## Repository layout

```
apps/
  woocommerce-mcp/   WooCommerce MCP server        (README.md, scripts/integration_check.py)
  gateway/           MCP gateway                   (README.md)
  notes-mcp/         Notes MCP server
  dashboard/         BFF + React SPA + chat agent  (README.md)
deployments/helm/
  woocommerce/       WordPress, WooCommerce, MariaDB, WooCommerce MCP  (README.md, scripts/)
  gateway/           gateway, PostgreSQL, Notes, GitHub MCP, dashboard (README.md, scripts/)
scripts/prefetch-wheels.sh   hash-checked wheel prefetch for slow Docker networks
spikes/hello-mcp/            original MCP spike
```

Component documentation:
[WooCommerce chart](deployments/helm/woocommerce/README.md) ·
[WooCommerce MCP](apps/woocommerce-mcp/README.md) ·
[Gateway](apps/gateway/README.md) ·
[Gateway chart](deployments/helm/gateway/README.md) ·
[Dashboard](apps/dashboard/README.md)
