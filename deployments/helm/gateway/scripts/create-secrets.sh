#!/usr/bin/env bash
# Secrets for the gateway chart. Values are generated locally, never printed,
# and loaded with --from-file from a private temp dir that is then deleted.
#
#   scripts/create-secrets.sh [namespace]
#       Creates (if missing): gateway-secrets (key-pepper), gateway-postgres
#       (password), gateway-upstream-credentials (notes-token).
#   scripts/create-secrets.sh set-github-token [namespace] < token.txt
#       Adds/replaces github-token in gateway-upstream-credentials, read from
#       stdin (use a fine-grained, read-only PAT). Not echoed, not in argv.
#
# Rotating key-pepper invalidates every gateway API key.
set -euo pipefail

cmd="create"
if [ "${1:-}" = "set-github-token" ]; then cmd="github"; shift; fi
NS="${1:-urumi-gateway}"
CREDS=gateway-upstream-credentials

command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
umask 077
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
kubectl get namespace "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS"

if [ "$cmd" = "github" ]; then
  if [ -t 0 ]; then
    printf 'GitHub token (input hidden): ' >&2
    IFS= read -rs token; echo >&2
  else
    IFS= read -r token
  fi
  case "$token" in
    github_pat_*|ghp_*) ;;
    *) echo "that does not look like a GitHub token (github_pat_... or ghp_...)" >&2; exit 1 ;;
  esac
  printf '%s' "$token" > "$TMP/github-token"; unset token
  b64=$(base64 < "$TMP/github-token" | tr -d '\n')
  printf '{"data":{"github-token":"%s"}}' "$b64" > "$TMP/patch.json"; unset b64
  if kubectl -n "$NS" get secret "$CREDS" >/dev/null 2>&1; then
    kubectl -n "$NS" patch secret "$CREDS" --type merge --patch-file "$TMP/patch.json" >/dev/null
  else
    kubectl -n "$NS" create secret generic "$CREDS" --from-file=github-token="$TMP/github-token" >/dev/null
  fi
  echo "github-token stored in secret/$CREDS ($NS). The gateway reads it on the next GitHub call."
  exit 0
fi

command -v openssl >/dev/null || { echo "openssl not found" >&2; exit 1; }
# No trailing newline: values are used verbatim as env vars and bearer tokens.
rand() { openssl rand -base64 96 | tr -d '\n/+=' | cut -c1-"$1" | tr -d '\n'; }

create() { # name key...
  local name="$1"; shift
  if kubectl -n "$NS" get secret "$name" >/dev/null 2>&1; then
    echo "secret/$name already exists in $NS, leaving it unchanged"; return
  fi
  local args=()
  for key in "$@"; do args+=("--from-file=$key=$TMP/$key"); done
  kubectl -n "$NS" create secret generic "$name" "${args[@]}"
}

rand 64 > "$TMP/key-pepper";      create gateway-secrets key-pepper
rand 40 > "$TMP/password";        create gateway-postgres password
rand 48 > "$TMP/notes-token"
if kubectl -n "$NS" get secret "$CREDS" >/dev/null 2>&1; then
  # The credentials Secret may already exist (e.g. github-token added first): add only missing keys.
  if [ -z "$(kubectl -n "$NS" get secret "$CREDS" -o jsonpath='{.data.notes-token}')" ]; then
    printf '{"data":{"notes-token":"%s"}}' "$(base64 < "$TMP/notes-token" | tr -d '\n')" > "$TMP/patch.json"
    kubectl -n "$NS" patch secret "$CREDS" --type merge --patch-file "$TMP/patch.json" >/dev/null
    echo "added notes-token to secret/$CREDS"
  else
    echo "secret/$CREDS already has notes-token, leaving it unchanged"
  fi
else
  create "$CREDS" notes-token
fi
echo "Done. Add the GitHub token with:  $0 set-github-token $NS"
