# woocommerce Helm chart

WordPress + WooCommerce + MariaDB for the Urumi MCP Gateway. Defaults target the
local k3d cluster `urumi`. `values-vps.example.yaml` shows the overrides for a
single-node k3s VPS. Configuration is done with Helm values only, with no Kustomize.

| Component | Kind | Exposure | Storage |
|-----------|------|----------|---------|
| MariaDB 11.4 | StatefulSet + ClusterIP Service + NetworkPolicy | internal only; ingress limited to WordPress and init pods | `data-<release>-woocommerce-mariadb-0` PVC (5Gi) |
| WordPress 7.1 (Apache, PHP 8.3) | Deployment (Recreate) + ClusterIP Service | Traefik Ingress `shop.localtest.me` | `<release>-woocommerce-wp-content` PVC (5Gi), kept on uninstall |
| Init | post-install/post-upgrade hook Job (`wordpress:cli`) | none | mounts the wp-content PVC |

Only `wp-content` (plugins, themes, uploads) is persisted. WordPress core comes
from the image on every pod start, so bumping `wordpress.image.tag` upgrades core.

## PHP runtime security

* Both PHP images are pinned by multi-arch index digest (`*.image.digest`), so
  a re-pull or a new node cannot silently switch builds. Both run **PHP 8.3.36**,
  which fixes CVE-2026-109618 and CVE-2026-109619 (php.net, 21 Oct 2026). This
  was verified on linux/arm64 and linux/amd64: `PHP_SHA256` matches php.net's
  published checksum for `php-8.3.36.tar.xz`, and Apache's `apache2handler`
  reports 8.3.36.
* `wordpress.phpIni` is rendered into `/usr/local/etc/php/conf.d/zz-urumi.ini`
  for the web and init containers. It sets
  `session.upload_progress.enabled = 0`, as recommended by the php.net advisory
  for CVE-2026-109619 and the 23 Oct 2026 follow-up. The advisory requires
  php.ini or server configuration, not `ini_set()` or `.user.ini`, and a worker
  restart; a checksum annotation rolls the pod when the setting changes. This
  is defense in depth, not a substitute for the patched PHP.
* Before changing an image, verify inside the image that `php -v` and the
  `PHP_SHA256` env match a fixed release, then update the tag and the digest
  together.

## WooCommerce MCP server (`mcp.*`)

The chart also deploys `apps/woocommerce-mcp`: a ClusterIP Service with no
Ingress, and a NetworkPolicy that admits only pods labelled
`urumi.dev/woocommerce-mcp-client: "true"` in this namespace. Its egress is
limited to DNS and Traefik's TLS port. It reuses the `woocommerce-api-key`
Secret through `secretKeyRef`. It calls WooCommerce through Traefik's
in-cluster Service over verified TLS, so WooCommerce's HTTPS requirement for
Basic auth holds. See `apps/woocommerce-mcp/README.md`.

### Local TLS for the store (`ingress.tls`)

`scripts/create-local-tls.sh` issues a dev CA and a certificate for
`shop.localtest.me` into the Secrets `shop-tls` (served by Traefik) and
`shop-tls-ca` (trusted by the MCP server only). With `ingress.tls` set, the
chart renders a websecure-only TLS Ingress plus a web-entrypoint Ingress that
only redirects to HTTPS. Browsers and curl still need `--cacert` (or `-k`)
locally:

```sh
kubectl -n woocommerce get secret shop-tls-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > /tmp/urumi-ca.crt
curl --cacert /tmp/urumi-ca.crt https://shop.localtest.me:8443/
```

### API key scope

The role `urumi_product_api` (display name "Urumi store API") holds the
product capabilities plus `read_private_shop_orders`, `edit_shop_orders` and
`edit_others_shop_orders`, which is the minimum for listing, reading and
status-updating orders. WooCommerce order statuses are non-public, so HPOS
maps `edit_post` on an order to `edit_others_shop_orders` only. It has no
customers, coupons, settings, system status, reports, order create/delete or
refund capabilities.

### Demo orders (`init.demoData.enabled`, default false)

`helm upgrade ... --set init.demoData.enabled=true` seeds three synthetic
orders (fake `example.invalid` customer, no payment) with
`files/seed-demo-orders.php`. The IDs are stored in the `urumi_demo_orders`
option and printed in the init Job log. `status-test` is the only order the
integration check may change. Local/dev only. With this Helm 4 build, a later
plain `helm upgrade` with no `--set` or `-f` keeps the previous release's
`--set` values, so run the next upgrade with `--reset-values` to turn seeding
back off. Seeding is idempotent either way.

## Secrets (required, never committed)

The chart only references Secrets by name. Create them before installing:

```sh
deployments/helm/woocommerce/scripts/create-secrets.sh woocommerce
```

The script generates random values, does not print them, and leaves existing
Secrets untouched. Delete a Secret first to rotate it.

| Secret | Keys | Used by |
|--------|------|---------|
| `woocommerce-db` | `mariadb-root-password`, `mariadb-password` | MariaDB, WordPress, init Job |
| `woocommerce-wordpress` | `admin-password`, `auth-key`, `secure-auth-key`, `logged-in-key`, `nonce-key`, `auth-salt`, `secure-auth-salt`, `logged-in-salt`, `nonce-salt` | WordPress, init Job |
| `woocommerce-api-key` | `consumer-key` (`ck_` + 40 hex), `consumer-secret` (`cs_` + 40 hex) | init Job; later the WooCommerce MCP server |

The salts are fixed through the Secret because `wp-config.php` is not
persisted. Otherwise the image would randomise them on every restart and log
everyone out.

## How initialisation works

The init Job runs after every `helm install` and `helm upgrade`. Every step is
idempotent:

1. Copies WordPress core from the same `wordpress` image tag into an
   `emptyDir`. The `wordpress:cli` image ships no core files.
2. Waits for the database and for the WordPress pod to populate `wp-content`.
3. Runs `wp core install` only if WordPress is not installed yet, using a
   throwaway password. Then `sync-admin-password.php` sets the admin password
   from the mounted Secret file, on every run and only when it differs. The
   value never appears in argv or logs, and rotating the Secret plus a
   `helm upgrade` changes the password. Do not switch to `--prompt`: WP-CLI
   echoes the prompted value and the full command into the Job log.
4. Runs `wp plugin install woocommerce --version=<pinned>` and then
   `wp plugin activate woocommerce`. Activation runs `WC_Install::install`.
   Next it runs `wp wc update` for database migrations, and sets
   `woocommerce_coming_soon=no` so logged-out visitors can see the storefront.
5. Recreates the role `urumi_product_api` with only the capabilities listed in
   `init.apiKey.capabilities`, and creates or updates the user `urumi-api`.
6. Upserts the REST key from `woocommerce-api-key` with
   `wp eval-file upsert-api-key.php`. **WooCommerce has no WP-CLI command for
   creating REST keys** (checked against WooCommerce 11.2.1: its `wc` commands
   are REST CRUD plus `com`, `hpos`, `tool`, `tracker`, `update` and `palt`).
   The script therefore writes the same row that WooCommerce's own
   `WC_Auth::create_keys()` writes: `wc_api_hash(ck)`, the plain-text secret,
   and `truncated_key`.

### Permissions of the API key

* **Key scope `read_write`.** `POST /products` needs `write`, and the spike
  reads products back to verify them, which needs `read`. Set
  `init.apiKey.permissions=write` if you only ever create products.
* **Owning user: role `urumi_product_api`, not admin and not shop_manager.**
  WooCommerce checks the user's capabilities in addition to the key scope. The
  role grants `read`, `read_private_products` (list), `publish_products`
  (create), `edit_products`, `edit_published_products` and
  `assign_product_terms`. It has no access to orders, customers, coupons,
  settings or plugins.

### HTTPS is required for the REST API

WooCommerce accepts `consumer_key`/`consumer_secret` Basic auth only when
`is_ssl()` is true; over plain HTTP it requires OAuth 1.0a signatures. Traefik
terminates TLS and sends `X-Forwarded-Proto: https`, which the wordpress image
maps to `$_SERVER['HTTPS']`. That is why `siteUrl` is
`https://shop.localtest.me:8443` on k3d (host port 8443 maps to Traefik 443) and
the chart refuses an `http://` siteUrl while the API key is enabled. Locally
Traefik serves its default self-signed certificate, so use `curl -k`.

## Install (local k3d)

```sh
cd ~/urumi
deployments/helm/woocommerce/scripts/create-secrets.sh woocommerce
deployments/helm/woocommerce/scripts/create-local-tls.sh woocommerce
# build + import the MCP image first (apps/woocommerce-mcp/README.md), or --set mcp.enabled=false
helm lint deployments/helm/woocommerce
helm upgrade --install shop deployments/helm/woocommerce -n woocommerce \
  --wait --timeout 15m
kubectl -n woocommerce logs job/shop-woocommerce-init
```

`--wait` makes Helm wait for MariaDB and WordPress to become ready before the
post-install Job runs. The Job downloads WooCommerce from downloads.wordpress.org,
so the cluster needs outbound internet access.

## Test

```sh
NS=woocommerce
CK=$(kubectl -n $NS get secret woocommerce-api-key -o jsonpath='{.data.consumer-key}' | base64 -d)
CS=$(kubectl -n $NS get secret woocommerce-api-key -o jsonpath='{.data.consumer-secret}' | base64 -d)

# Storefront (HTTPS, self-signed locally)
curl -sk -o /dev/null -w '%{http_code}\n' https://shop.localtest.me:8443/shop/

# Create a product, then read it back
curl -sk -u "$CK:$CS" -H 'Content-Type: application/json' \
  -d '{"name":"Spike product","type":"simple","regular_price":"9.99"}' \
  https://shop.localtest.me:8443/wp-json/wc/v3/products
curl -sk -u "$CK:$CS" 'https://shop.localtest.me:8443/wp-json/wc/v3/products?search=Spike'

# MariaDB must not be reachable from outside the cluster
kubectl -n $NS get svc shop-woocommerce-mariadb   # TYPE ClusterIP, no node port
```

Admin password:
`kubectl -n woocommerce get secret woocommerce-wordpress -o jsonpath='{.data.admin-password}' | base64 -d`

## VPS (k3s)

```sh
cp deployments/helm/woocommerce/values-vps.example.yaml values-vps.yaml   # edit hosts
deployments/helm/woocommerce/scripts/create-secrets.sh woocommerce
helm upgrade --install shop deployments/helm/woocommerce -n woocommerce \
  -f values-vps.yaml --wait --timeout 15m
```

The example assumes cert-manager with a `letsencrypt-prod` ClusterIssuer.
**The VPS path has not been tested yet.** Locally, the chart's Traefik
`redirectScheme` Middleware (`ingress.httpsRedirect`) sends
`http://shop.localtest.me:8080` to `https://shop.localtest.me:8443`. Once the
Ingress has a `tls` block, Traefik may build only a TLS router. If plain HTTP
then returns 404 on the VPS, configure the redirect on Traefik's `web`
entrypoint instead.

## Reproducing the local deployment from scratch

Order matters, and every step is idempotent:

```sh
cd ~/urumi
deployments/helm/woocommerce/scripts/create-secrets.sh woocommerce     # DB, WordPress, API-key Secrets (random)
deployments/helm/woocommerce/scripts/create-local-tls.sh woocommerce   # shop-tls + shop-tls-ca (dev CA)
(cd apps/woocommerce-mcp && docker build -t urumi/woocommerce-mcp:0.1.0 . \
  && k3d image import urumi/woocommerce-mcp:0.1.0 -c urumi)
helm lint deployments/helm/woocommerce
helm upgrade --install shop deployments/helm/woocommerce -n woocommerce --wait --timeout 15m
# optional, dev only: seed demo orders, then return to defaults
helm upgrade shop deployments/helm/woocommerce -n woocommerce --set init.demoData.enabled=true --wait
helm upgrade shop deployments/helm/woocommerce -n woocommerce --reset-values --wait
```

Notes:

* **Local TLS:** the dev CA's certificate is the only trust anchor. Re-running
  `create-local-tls.sh --rotate` re-issues both certificates; then restart the
  MCP pod (`kubectl -n woocommerce rollout restart deploy/shop-woocommerce-mcp`)
  so it reads the new `ca.crt`. Use `--cacert` or `-k` with curl and browsers.
* **Helm values carry-over:** this Helm 4 build reused the previous release's
  `--set` values on a plain `helm upgrade`. Check with
  `helm -n woocommerce get values shop`, and use `--reset-values` to return to
  the chart defaults.
* **Secrets are cluster state only.** Nothing in the repo contains credential
  values or private keys. A fresh cluster gets fresh random values from the
  scripts.

### Test data left in the current local store (2026-10-24)

| Kind | ID | Name / status | Created by |
|------|----|---------------|------------|
| product | 11 | Spike product | first manual REST spike |
| product | 13 | PHP patch check 1792822434 | PHP 8.3.36 regression test |
| product | 14 | Urumi demo item | demo seed |
| product | 18, 19 | MCP integration &lt;random&gt; | `integration_check.py` runs |
| order | 15 | processing (demo-processing) | demo seed |
| order | 16 | completed (demo-completed) | demo seed |
| order | 17 | pending (status-test; used for pending ↔ on-hold tests, restored to pending) | demo seed |

All of them are synthetic. Customers use `@example.invalid`, and there are no
payments. Delete them from wp-admin before treating this store as anything but
a dev instance.

## Uninstall

```sh
helm uninstall shop -n woocommerce
```

The wp-content PVC is kept (`helm.sh/resource-policy: keep`), and the
MariaDB StatefulSet PVC is never deleted by Helm. Delete both PVCs and the
Secrets by hand to wipe everything.
