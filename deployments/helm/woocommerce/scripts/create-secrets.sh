#!/usr/bin/env bash
# Create the Secrets the woocommerce chart expects, with freshly generated
# random values. Values never touch argv or the repo: they are written to a
# private temp dir, loaded with --from-file, and the dir is removed.
#
# Usage: scripts/create-secrets.sh [namespace]      (default: woocommerce)
# Existing Secrets are left untouched; delete one first to rotate it.
set -euo pipefail

NS="${1:-woocommerce}"

command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl not found" >&2; exit 1; }

umask 077
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

rand() { openssl rand -base64 "$1" | tr -d '\n/+=' | cut -c1-"$2"; }

kubectl get namespace "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS"

create() { # name key...
  local name="$1"; shift
  if kubectl -n "$NS" get secret "$name" >/dev/null 2>&1; then
    echo "secret/$name already exists in $NS, leaving it unchanged"
    return
  fi
  local args=()
  for key in "$@"; do args+=("--from-file=$key=$TMP/$key"); done
  kubectl -n "$NS" create secret generic "$name" "${args[@]}"
}

rand 48 32 > "$TMP/mariadb-root-password"
rand 48 32 > "$TMP/mariadb-password"
create woocommerce-db mariadb-root-password mariadb-password

rand 48 24 > "$TMP/admin-password"
salts=(auth-key secure-auth-key logged-in-key nonce-key auth-salt secure-auth-salt logged-in-salt nonce-salt)
for s in "${salts[@]}"; do rand 96 64 > "$TMP/$s"; done
create woocommerce-wordpress admin-password "${salts[@]}"

# Same format WooCommerce generates: prefix + 40 lowercase hex characters.
printf 'ck_%s' "$(openssl rand -hex 20)" > "$TMP/consumer-key"
printf 'cs_%s' "$(openssl rand -hex 20)" > "$TMP/consumer-secret"
create woocommerce-api-key consumer-key consumer-secret

echo "Done. Read a value when needed, e.g.:"
echo "  kubectl -n $NS get secret woocommerce-wordpress -o jsonpath='{.data.admin-password}' | base64 -d"
