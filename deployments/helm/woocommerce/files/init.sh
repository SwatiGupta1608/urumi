#!/bin/sh
# Idempotent WordPress + WooCommerce initialisation. Runs in the official
# wordpress:cli image as www-data (uid 33) against a docroot that the init
# container populated from the matching wordpress image, with the shared
# wp-content PVC mounted. Safe to re-run on every helm install/upgrade.
set -eu

log() { printf '[init] %s\n' "$*"; }

: "${WP_URL:?}" "${WP_TITLE:?}" "${WP_ADMIN_USER:?}" "${WP_ADMIN_EMAIL:?}"
: "${WP_ADMIN_PASSWORD_FILE:?}" "${WC_VERSION:?}"

log "waiting for database"
i=0
until wp db check --quiet >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -ge 60 ] && { log "database not reachable"; wp db check; exit 1; }
  sleep 5
done

# The WordPress pod copies bundled themes/plugins into the shared wp-content
# volume on its first start. Wait so core install picks up a default theme.
log "waiting for wp-content to be populated by the WordPress pod"
i=0
until [ -n "$(ls -A wp-content/themes 2>/dev/null)" ]; do
  i=$((i + 1))
  [ "$i" -ge 60 ] && { log "wp-content/themes is still empty"; exit 1; }
  sleep 5
done

if wp core is-installed; then
  log "WordPress core already installed"
else
  log "installing WordPress core"
  # Throwaway password; the real one is synced from the Secret file below.
  # (--prompt would echo the value and the full command line into the logs.)
  wp core install \
    --url="$WP_URL" \
    --title="$WP_TITLE" \
    --admin_user="$WP_ADMIN_USER" \
    --admin_email="$WP_ADMIN_EMAIL" \
    --admin_password="$(head -c 32 /dev/urandom | base64)" \
    --skip-email \
    --quiet
fi
wp eval-file /scripts/sync-admin-password.php

# Pretty permalinks so /wp-json/ works (the image ships a matching .htaccess).
wp rewrite structure '/%postname%/' --quiet

current="$(wp plugin get woocommerce --field=version 2>/dev/null || true)"
if [ "$current" = "$WC_VERSION" ]; then
  log "WooCommerce $WC_VERSION already installed"
else
  log "installing WooCommerce $WC_VERSION (found: ${current:-none})"
  wp plugin install woocommerce --version="$WC_VERSION" --force
fi
if ! wp plugin is-active woocommerce; then
  # Activation runs WC_Install::install (tables, roles, pages).
  wp plugin activate woocommerce
fi
# Apply any pending WooCommerce database migrations (no-op when current).
wp wc update

if [ "${WC_DISABLE_COMING_SOON:-true}" = "true" ]; then
  wp option update woocommerce_coming_soon no --quiet
fi

if [ "${WC_DEMO_ORDERS:-false}" = "true" ]; then
  log "seeding synthetic demo orders"
  wp eval-file /scripts/seed-demo-orders.php
fi

if [ "${WC_API_KEY_ENABLED:-false}" != "true" ]; then
  log "API key provisioning disabled"
  exit 0
fi

: "${WC_API_ROLE:?}" "${WC_API_USER:?}" "${WC_API_USER_EMAIL:?}" "${WC_API_CAPS:?}"

# Recreate the role so its capability set always equals the chart values.
# Users keep the role slug, so existing assignments stay valid.
if wp role exists "$WC_API_ROLE" >/dev/null 2>&1; then
  wp role delete "$WC_API_ROLE" --quiet
fi
wp role create "$WC_API_ROLE" "${WC_API_ROLE_NAME:-$WC_API_ROLE}" --quiet
# shellcheck disable=SC2086 # intentional word splitting of the capability list
wp cap add "$WC_API_ROLE" $WC_API_CAPS --quiet

if wp user get "$WC_API_USER" --field=ID >/dev/null 2>&1; then
  wp user set-role "$WC_API_USER" "$WC_API_ROLE" --quiet
else
  log "creating API user $WC_API_USER"
  # Random password is generated and discarded; the user only authenticates
  # through the WooCommerce REST key.
  wp user create "$WC_API_USER" "$WC_API_USER_EMAIL" --role="$WC_API_ROLE" --porcelain >/dev/null
fi

log "upserting WooCommerce REST API key"
wp eval-file /scripts/upsert-api-key.php

log "done"
