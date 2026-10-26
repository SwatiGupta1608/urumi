# gateway Helm chart

Deploys the Urumi MCP Gateway (`apps/gateway`) with its own PostgreSQL, the
Notes MCP server (`apps/notes-mcp`) and the GitHub MCP server, all in namespace
`urumi-gateway`. It reuses the WooCommerce MCP server from the `woocommerce`
release instead of duplicating it. Nothing is exposed through an Ingress. The dashboard (`apps/dashboard/README.md`) is reached with
`kubectl -n urumi-gateway port-forward svc/gateway-dashboard 18090:8090`.

| Component | Kind | Reachable from | May reach |
|-----------|------|----------------|-----------|
| `gateway` | Deployment + ClusterIP :8080 | pods labelled `urumi.dev/gateway-client=true` (and `kubectl port-forward`) | DNS, `gateway-postgres:5432`, `gateway-notes:8000`, `gateway-github:8082`, WooCommerce MCP pods in `woocommerce`:8000 |
| `gateway-postgres` | StatefulSet + PVC 2Gi (PostgreSQL 17.11) | gateway only | nothing |
| `gateway-notes` | Deployment + PVC 1Gi (SQLite) | gateway only | nothing |
| `gateway-dashboard` | Deployment (React SPA + BFF, `apps/dashboard`) | nobody except `kubectl port-forward` (ingress allow-list empty) | DNS, `gateway:8080` |
| `gateway-github` | Deployment (`github-mcp-server v2.0.2 http --read-only`) | gateway only | DNS, public HTTPS (private/cluster ranges excluded) |

All containers run as non-root with a read-only root filesystem, drop all
capabilities, use `RuntimeDefault` seccomp, and have no service-account token.
Images are pinned by digest where they come from a registry. The locally built
images (`urumi/gateway`, `urumi/notes-mcp`) are imported into k3d.

The WooCommerce chart's `mcp.networkPolicy.clientNamespaceSelector` admits
labelled pods from `woocommerce` and `urumi-gateway`. That is the only change
on the WooCommerce side.

## Secrets (never in values or git)

| Secret | Keys | Used by |
|--------|------|---------|
| `gateway-secrets` | `key-pepper` | gateway (HMAC pepper for API-key hashes) |
| `gateway-postgres` | `password` | PostgreSQL, gateway |
| `gateway-upstream-credentials` | `notes-token`, `github-token` (optional) | gateway (mounted as credential files); notes MCP (`notes-token` env) |

```sh
deployments/helm/gateway/scripts/create-secrets.sh urumi-gateway
deployments/helm/gateway/scripts/create-secrets.sh set-github-token urumi-gateway   # paste a fine-grained read-only PAT (hidden)
```

## Install / upgrade (local k3d)

```sh
cd ~/urumi
# If downloads inside Docker are slow, prefetch the locked wheels on the host
# (hash-checked against uv.lock); otherwise drop the --build-context flags.
scripts/prefetch-wheels.sh apps/gateway && scripts/prefetch-wheels.sh apps/notes-mcp
docker build --build-context wheels=apps/gateway/.wheels -t urumi/gateway:0.1.0 apps/gateway
docker build --build-context wheels=apps/notes-mcp/.wheels -t urumi/notes-mcp:0.1.0 apps/notes-mcp
k3d image import urumi/gateway:0.1.0 urumi/notes-mcp:0.1.0 -c urumi

deployments/helm/gateway/scripts/create-secrets.sh urumi-gateway
helm lint deployments/helm/gateway
helm template gateway deployments/helm/gateway -n urumi-gateway | less      # review
helm upgrade --install gateway deployments/helm/gateway -n urumi-gateway --wait --timeout 10m
# let the gateway reach the WooCommerce MCP server (values change in that chart)
helm upgrade shop deployments/helm/woocommerce -n woocommerce --reset-values --wait

kubectl -n urumi-gateway exec -it deploy/gateway -- python -m gateway.cli create-admin --name <you>
```

Then register the upstreams as admin (see `apps/gateway/README.md`):

```sh
kubectl -n urumi-gateway port-forward svc/gateway 18080:8080 &
H="Authorization: Bearer $ADMIN_KEY"
curl -s -H "$H" -H 'Content-Type: application/json' localhost:18080/api/v1/upstreams -d '{"slug":"woocommerce","display_name":"WooCommerce","url":"http://shop-woocommerce-mcp.woocommerce.svc.cluster.local:8000/mcp","admin_only_tools":["create_product","update_order_status"]}'
curl -s -H "$H" -H 'Content-Type: application/json' localhost:18080/api/v1/upstreams -d '{"slug":"notes","display_name":"Team notes","url":"http://gateway-notes.urumi-gateway.svc.cluster.local:8000/mcp","credential_ref":"notes-token"}'
curl -s -H "$H" -H 'Content-Type: application/json' localhost:18080/api/v1/upstreams -d '{"slug":"github","display_name":"GitHub read-only","url":"http://gateway-github.urumi-gateway.svc.cluster.local:8082/","credential_ref":"github-token"}'
```

## k3s VPS

Only values change: storage class and sizes, `gateway.settings.upstreamAllowedCidrs`
(if the cluster CIDRs differ), and the upstream egress list. Keep the gateway
ClusterIP-only until a dashboard with its own authentication fronts it.

## Uninstall

`helm uninstall gateway -n urumi-gateway` keeps the Notes PVC (`resource-policy: keep`)
and the PostgreSQL PVC (StatefulSet claims are never deleted by Helm).
