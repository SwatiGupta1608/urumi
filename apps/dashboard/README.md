# Urumi dashboard (MVP)

A React + TypeScript SPA served by a small FastAPI **backend-for-frontend
(BFF)** in the same pod. It manages the Urumi MCP Gateway through the gateway's
existing REST API (`/api/v1`). Deployed by `deployments/helm/gateway`
(`dashboard.*` values), internal only.

```
browser ──HttpOnly session cookie──▶ dashboard BFF (urumi-gateway ns) ──Bearer <user's own key>──▶ gateway /api/v1
         (no API key in the browser)   keeps the key in memory per session     (authoritative authz)
```

## Why a BFF

The gateway authenticates with personal API keys and has no sessions. It is
ClusterIP-only, and its NetworkPolicy admits only labelled pods. Exposing it
to browsers would mean putting keys in browser storage or opening the gateway
publicly. The BFF avoids both:

* **Login**: you paste **your own** gateway key once. The BFF verifies it with
  `GET /api/v1/me` and keeps it only in server memory, bound to a random
  session id. The cookie is `HttpOnly`, `SameSite=Strict` and `Secure` when
  served over HTTPS. The key is never returned to the browser, written to
  browser storage (an ESLint rule bans `localStorage`, `sessionStorage` and
  IndexedDB), or logged.
* **Identity maps 1:1** to gateway users. The session is the key's user,
  re-checked by the gateway on every request.
* **Proxy allow-list**: only the gateway routes the UI uses (method + path)
  are forwarded. Anything else returns 404/405, and only known filters are
  passed on to `/audit`.
* **Session end**: a gateway 401 (revoked key, disabled user) destroys the
  session, and the UI explains why. Sessions also end after 30 minutes idle or
  8 hours total, and on BFF restart (in-memory, single replica).
* **CSRF**: writes need the header `X-Urumi-Dashboard: 1`, and any `Origin`
  header must match the host. Combined with `SameSite=Strict`, this blocks
  cross-site requests. The page sends a strict CSP, `X-Frame-Options: DENY`
  and `no-store`.
* **Authorisation stays in the gateway.** The UI hides admin pages from
  members only for usability. Members get 403 from the gateway if they try
  anyway (tested).

## Screens

| Screen | Data (gateway API) | Who |
|--------|--------------------|-----|
| Sign-in | `POST /bff/login` → `GET /api/v1/me` | everyone |
| Overview | `/bff/health` (gateway readiness), `/api/v1/upstreams` (admin) or `/api/v1/tools` counts (member). Auto-refresh 15s | everyone |
| Upstreams | list / register / edit / enable-disable / remove / rediscover (`/api/v1/upstreams…`), tools with `slug__tool` names and admin-only badges (`/api/v1/tools`) | admin |
| Team & keys | users (`/users`), create user, role and disable (`PATCH /users/{id}`), keys per user, issue (raw key shown **once**, copy button, held in memory only), revoke | admin |
| My account | own profile and key metadata (`/me`, `/me/keys`) | member |
| Audit log | `/api/v1/audit` with outcome, user, upstream and event filters, "load more" up to 500; denials shown separately from failures | admin |
| Chat | LLM agent over your gateway tools, with tool activity, confirm/cancel for changes, example prompts and reset (`/bff/chat/*`, see below) | everyone |

## Chat agent

The agent runs **inside the BFF**. The browser sends text and confirm/cancel
decisions only; it never sees the gateway key or the LLM key.

```
browser ──/bff/chat/*──▶ BFF ──(provider key)──▶ OpenAI-compatible LLM
                          └──MCP POST /mcp, Bearer <your own gateway key>──▶ gateway ──▶ upstream MCP servers
```

* **Tools are discovered per user** through the gateway (`tools/list`).
  Members never see admin-only tools. The gateway authorises and audits every
  call under the signed-in user.
* **Every model call is validated.** A call is refused (and reported to the
  model) when:
  * the tool name is unknown or was not offered,
  * the arguments are not JSON or fail the tool's JSON schema (`jsonschema`),
  * the per-turn limit is reached. Limits: 6 model steps and 8 tool calls per
    turn; tool results are cut to 6000 characters for the model and 300 for
    the UI.
* **Confirmation** is required for every tool unless an operator has
  reviewed that exact tool and listed it in `dashboard.chat.readOnlyTools`.
  Upstream `readOnlyHint` metadata is not trusted on its own; it never skips
  confirmation, but it can force one: a listed tool that its upstream now
  marks `readOnlyHint: false` or `destructiveHint: true` is confirmed again.
  The reviewed list has 25 tools:
  * WooCommerce `list_products`, `list_orders`, `get_order`: only `GET`
    requests in `apps/woocommerce-mcp`.
  * Notes `list_notes`, `get_note`: `SELECT`-only.
  * 20 GitHub `get_*`, `list_*`, `search_*` and `issue_read` tools: the server
    runs with `--read-only`, so writes are blocked server-side.

  WooCommerce `create_product` and `update_order_status`, Notes
  `create_note` and `delete_note`, and any newly discovered tool always need
  confirmation. The list matches gateway tool names, so it trusts whichever
  upstream an admin registers under that slug. The BFF returns a
  preview with a pending action that is:
  * bound to the session,
  * bound to an exact digest of tool + arguments,
  * valid for 5 minutes,
  * single-use (`pending → executing → done | cancelled | expired`; a double
    click gets 409).

  On confirm, the tool's availability and the arguments are re-checked
  against a fresh gateway tool list. A cancelled or expired action is never
  executed.
* **Untrusted output**: tool results are labelled as untrusted data for the
  model, and the UI renders everything as plain text.
* **State** is in memory per BFF session (bounded to 60 history messages and
  about 60k characters of context). It is cleared on logout, session expiry
  or a revoked key, and lost on restart. Order-tool results (which can contain
  customer names and emails) live only there.

### Configure a real provider

The default chart has **no provider**. Chat then shows "unavailable" and
`POST /bff/chat/messages` returns `503 provider_not_configured`. Nothing is
simulated. To enable chat:

```sh
# 1. Store the provider API key out-of-band (never in values or git):
printf '%s' "$KEY_FROM_YOUR_PASSWORD_MANAGER" | kubectl -n urumi-gateway create secret generic dashboard-llm --from-file=api-key=/dev/stdin
# 2. Point the chart at an OpenAI-compatible Chat Completions endpoint with tool calling:
helm upgrade gateway deployments/helm/gateway -n urumi-gateway --reuse-values \
  --set dashboard.chat.provider.baseUrl=https://api.openai.com/v1 \
  --set dashboard.chat.provider.model=<model-with-tool-calling>
```

Setting `baseUrl` also adds an HTTPS-only (443) egress rule to the
dashboard's NetworkPolicy, with private and cluster ranges excluded. Without
it, the dashboard can still reach only DNS and the gateway.

## API gaps (not faked in the UI)

1. **Upstream health for members**: `/upstreams` is admin-only, so members
   only see tool counts per server.
2. **Self-service keys**: members cannot create or revoke their own keys
   (there are no `/me/keys` write routes), so they ask an admin.
3. **Key expiry**: keys have no expiry. "Expired" is handled at the session
   level (idle and absolute TTL).
4. **Audit pagination**: there is no cursor or offset, only `limit` (max
   500) plus filters. The UI offers "load more" up to 500.
5. **Deleting users**: there is no delete route; disable is used instead.
6. **Gateway health detail**: `/readyz` only reports database readiness.
   Per-upstream health comes from `/upstreams`.
7. **Server settings**: timeouts and SSRF policy come from env and Helm
   values; there is no API to change them.

## Develop and test

```sh
# BFF
cd apps/dashboard && uv sync && uv run pytest -q
# SPA
cd apps/dashboard/web && npm ci && npm run typecheck && npm run lint && npm test && npm run build
# Local dev: run the BFF against a port-forwarded gateway, then `npm run dev` (Vite proxies /api and /bff to :8090)
kubectl -n urumi-gateway port-forward svc/gateway 18080:8080 &
GATEWAY_URL=http://localhost:18080 DASHBOARD_SECURE_COOKIES=false DASHBOARD_STATIC_DIR=web/dist \
  uv run uvicorn --factory dashboard_bff.app:create_app --port 8090
```

## Build and deploy (k3d)

```sh
cd ~/urumi
(cd apps/dashboard/web && npm ci && npm run build)          # static SPA, no secrets
scripts/prefetch-wheels.sh apps/dashboard                    # optional: slow Docker network
docker build --build-context wheels=apps/dashboard/.wheels -t urumi/dashboard:0.1.0 apps/dashboard
k3d image import urumi/dashboard:0.1.0 -c urumi
helm upgrade gateway deployments/helm/gateway -n urumi-gateway --wait
kubectl -n urumi-gateway port-forward svc/gateway-dashboard 18090:8090
# open http://localhost:18090 and sign in with your own gateway API key
```

`dashboard.secureCookies` is `false` for plain-HTTP port-forwarding. Set it
to `true` once the dashboard is served over TLS. Before adding any Ingress,
add TLS, an authentication design (e.g. SSO in front of the BFF),
`dashboard.networkPolicy.ingressFrom` for the ingress controller, and shared
session storage if you run more than one replica.
