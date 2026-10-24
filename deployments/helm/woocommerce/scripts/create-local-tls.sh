#!/usr/bin/env bash
# Create a local development CA and a TLS certificate for the store hostname,
# and store them as Kubernetes Secrets:
#   <name>     kubernetes.io/tls  tls.crt / tls.key   -> served by Traefik (Ingress tls)
#   <name>-ca  Opaque             ca.crt              -> trusted by the WooCommerce MCP server only
#
# The CA private key is discarded after signing, so nothing else can be issued
# from this CA. Rotate by re-running with --rotate (re-issues CA and leaf).
#
# Usage: scripts/create-local-tls.sh [--rotate] [namespace] [hostname] [secret-name]
# Defaults: woocommerce shop.localtest.me shop-tls
# Works with OpenSSL and LibreSSL (macOS).
set -euo pipefail

ROTATE=false
if [ "${1:-}" = "--rotate" ]; then ROTATE=true; shift; fi
NS="${1:-woocommerce}"
HOST="${2:-shop.localtest.me}"
NAME="${3:-shop-tls}"

command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl not found" >&2; exit 1; }

if kubectl -n "$NS" get secret "$NAME" >/dev/null 2>&1 || kubectl -n "$NS" get secret "$NAME-ca" >/dev/null 2>&1; then
  if [ "$ROTATE" != true ]; then
    echo "secret/$NAME or secret/$NAME-ca already exists in $NS; use --rotate to re-issue"
    exit 0
  fi
  kubectl -n "$NS" delete secret "$NAME" "$NAME-ca" --ignore-not-found
fi

umask 077
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/ca.cnf" <<EOF
[req]
distinguished_name = dn
prompt = no
[dn]
CN = Urumi Local Dev CA ($HOST)
[v3_ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
EOF

cat > "$TMP/leaf.cnf" <<EOF
[req]
distinguished_name = dn
prompt = no
[dn]
CN = $HOST
[v3_leaf]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:$HOST
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
EOF

openssl genrsa -out "$TMP/ca.key" 2048 2>/dev/null
openssl req -x509 -new -key "$TMP/ca.key" -sha256 -days 825 \
  -config "$TMP/ca.cnf" -extensions v3_ca -out "$TMP/ca.crt"

openssl genrsa -out "$TMP/tls.key" 2048 2>/dev/null
openssl req -new -key "$TMP/tls.key" -config "$TMP/leaf.cnf" -out "$TMP/tls.csr"
openssl x509 -req -in "$TMP/tls.csr" -CA "$TMP/ca.crt" -CAkey "$TMP/ca.key" -CAcreateserial \
  -sha256 -days 397 -extfile "$TMP/leaf.cnf" -extensions v3_leaf -out "$TMP/tls.crt" 2>/dev/null

openssl verify -CAfile "$TMP/ca.crt" "$TMP/tls.crt" >/dev/null

kubectl get namespace "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS"
kubectl -n "$NS" create secret tls "$NAME" --cert="$TMP/tls.crt" --key="$TMP/tls.key"
kubectl -n "$NS" create secret generic "$NAME-ca" --from-file=ca.crt="$TMP/ca.crt"

echo "Issued $HOST (valid 397 days). To trust it with curl:"
echo "  kubectl -n $NS get secret $NAME-ca -o jsonpath='{.data.ca\\.crt}' | base64 -d > /tmp/urumi-ca.crt"
echo "  curl --cacert /tmp/urumi-ca.crt https://$HOST:8443/"
