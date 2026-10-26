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
| Chat | layout and tool-activity panel only. `src/api/chat.ts` defines the `ChatApi` boundary; no LLM yet | everyone |

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
