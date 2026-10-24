# woocommerce-mcp

An MCP server (Streamable HTTP, official Python SDK `mcp<2`, hosted in FastAPI)
that exposes a small, fixed set of WooCommerce operations. It is deployed by the
`deployments/helm/woocommerce` chart (`mcp.*` values) and is reachable only
inside the cluster.

| Tool | WooCommerce call | Limits |
|------|------------------|--------|
| `list_products` | `GET /products` | `page` 1-1000, `per_page` 1-50, optional `search` (1-100 chars) |
| `create_product` | `POST /products` (`type=simple`) | `name` 1-200 chars, no control chars; `regular_price` > 0, ≤ 1,000,000, ≤ 2 decimals; `description` ≤ 5000; `status` `publish`\|`draft` |
| `list_orders` | `GET /orders` | `page`, `per_page` as above; optional `status` ∈ pending, processing, on-hold, completed, cancelled, refunded, failed |
| `get_order` | `GET /orders/{id}` | `order_id` 1-10¹² (strict integer) |
| `update_order_status` | `GET` + `PUT /orders/{id}` | `status` ∈ pending, processing, on-hold, completed, cancelled, failed; returns previous/new status, no-op if unchanged |

`refunded` cannot be set: changing the status field records a refund
without moving any money. There is no generic request tool, and no tool
accepts URLs, paths, HTTP methods or credentials.

Every tool returns structured content (with an output schema). Failures come
back as MCP tool errors with actionable messages, such as validation errors,
`Not found in WooCommerce: Invalid ID.`, `... not permitted to do this (403)`,
timeouts with the configured limits, or TLS verification failures. They never
include credentials.

## How it reaches WooCommerce

WooCommerce accepts consumer-key Basic auth **only over HTTPS** (`is_ssl()`).
Inside the cluster, `shop.localtest.me` resolves to `127.0.0.1`, and the
WordPress Service is plain HTTP. The server therefore:

1. dials `https://traefik.kube-system.svc.cluster.local` (`WC_BASE_URL`),
2. sends SNI and `Host: shop.localtest.me` (`WC_TLS_SERVER_NAME`), so Traefik
   routes to the store Ingress, terminates TLS and forwards with
   `X-Forwarded-Proto: https`, which WordPress maps to `is_ssl()`,
3. verifies the certificate **for `shop.localtest.me`** against `WC_CA_FILE`.

Locally that CA is a throwaway dev CA created by
`deployments/helm/woocommerce/scripts/create-local-tls.sh`. Its private key is
discarded after signing. Only this one `httpx` client trusts it; verification
is never disabled. On a VPS with a public certificate, unset the CA
(`mcp.upstream.caSecret.name: ""`) to use the public `certifi` bundle.

Timeouts are 5s to connect and 15s to read (`WC_CONNECT_TIMEOUT`,
`WC_READ_TIMEOUT`, bounded to 0.5–60s). Redirects are never followed, and
proxy environment variables are ignored.

## Configuration (env)

| Variable | Required | Notes |
|----------|----------|-------|
| `WC_BASE_URL` | yes | `https://host[:port]` only |
| `WC_CONSUMER_KEY`, `WC_CONSUMER_SECRET` | yes | from Secret `woocommerce-api-key` via `secretKeyRef`; format-checked, never logged |
| `WC_TLS_SERVER_NAME` | no | SNI + cert hostname + Host header |
| `WC_CA_FILE` | no | CA bundle for this upstream only |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | no | DNS-rebinding protection allow-lists (the chart sets the Service names + localhost) |

Endpoints: `POST /mcp` (MCP), `GET /healthz` (liveness), `GET /readyz`
(readiness). Readiness does not depend on WooCommerce being up; upstream
problems are reported per tool call.

## Develop and test

```sh
cd apps/woocommerce-mcp
uv sync
uv run pytest -q                    # unit tests, all upstream calls mocked (respx)
```

## Build and deploy (local k3d)

```sh
cd ~/urumi/apps/woocommerce-mcp
docker build -t urumi/woocommerce-mcp:0.1.0 .
k3d image import urumi/woocommerce-mcp:0.1.0 -c urumi

cd ~/urumi
deployments/helm/woocommerce/scripts/create-local-tls.sh      # once: shop-tls + shop-tls-ca
helm lint deployments/helm/woocommerce
helm template shop deployments/helm/woocommerce -n woocommerce | less   # review
helm upgrade shop deployments/helm/woocommerce -n woocommerce --wait --timeout 10m
#   add  --set init.demoData.enabled=true  once to seed synthetic demo orders
```

## Who can call it

The NetworkPolicy only admits pods labelled
`urumi.dev/woocommerce-mcp-client: "true"` in the `woocommerce` namespace
(`mcp.networkPolicy.*`). The server itself may only reach cluster DNS and
Traefik's TLS port. It cannot reach WordPress HTTP, MariaDB, the Kubernetes
API or the internet. There is no Ingress for it.

There is **no MCP-level client authentication yet**. Access control is the
NetworkPolicy; the future gateway is expected to sit in front of it.

## Security notes for the gateway (read before building on this)

* **Personal data.** `get_order` returns the billing name and billing email;
  `list_orders` returns the billing name only. No addresses or phone numbers
  are returned. The gateway must authorise who may call order tools, audit
  those calls, and avoid logging tool results that contain this data.
* **Side effects.** `create_product` publishes to the live store by default.
  `update_order_status` can make WooCommerce send customer emails, adjust stock
  (e.g. on `cancelled`) and fire hooks/webhooks. The gateway should restrict it
  to authorised members, label it as state-changing with those side effects,
  and test only against demo orders.
* **Interim access control.** The NetworkPolicy plus the Host allow-list is a
  network boundary only. It does not replace member authentication and
  authorisation in the gateway.
* **TLS trust.** The pod mounts only `ca.crt` from Secret `shop-tls-ca`. The
  CA private key is never stored anywhere, because
  `scripts/create-local-tls.sh` discards it after signing. Verification is
  `CERT_REQUIRED` with hostname checking on. This was checked in the running
  pod: the correct name with the dev CA is accepted, the public CAs alone are
  rejected, and an unknown SNI name (which gets Traefik's default certificate)
  is rejected.

## Verify

```sh
# MCP Inspector (CLI) through a port-forward (port-forward bypasses NetworkPolicy by design)
kubectl -n woocommerce port-forward svc/shop-woocommerce-mcp 18000:8000 &
npx -y @modelcontextprotocol/inspector@2.10.1 --cli http://localhost:18000/mcp --transport http --method tools/list
# (UI: npx @modelcontextprotocol/inspector@2.10.1, transport "Streamable HTTP", URL http://localhost:18000/mcp)

# Controlled end-to-end check from an allowed in-cluster pod.
# Creates one uniquely named product. With --status-test-order it moves ONLY that
# order pending -> on-hold -> pending (aborts if it is not pending).
kubectl -n woocommerce logs job/shop-woocommerce-init -c wp-cli | grep 'demo orders'   # e.g. status-test: 17
kubectl -n woocommerce run mcp-integration --rm -i --restart=Never \
  --image=urumi/woocommerce-mcp:0.1.0 --image-pull-policy=Never \
  --labels=urumi.dev/woocommerce-mcp-client=true \
  --command -- python - --url http://shop-woocommerce-mcp:8000/mcp --status-test-order 17 --read-order 16 \
  < apps/woocommerce-mcp/scripts/integration_check.py
```

The demo order IDs are printed by the init Job (`kubectl -n woocommerce logs
job/shop-woocommerce-init`). Use the `status-test` ID for `--status-test-order`.
