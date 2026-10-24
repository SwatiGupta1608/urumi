#!/usr/bin/env bash
# Download the locked runtime wheels for an app on the host, for Docker builds
# where the container network is slow. Hashes come from uv.lock; the Dockerfile
# installs them with --require-hashes, so integrity is the same as a normal build.
#
# Usage: scripts/prefetch-wheels.sh <app-dir> [arch]      arch: aarch64 (default) | x86_64
# Then:  docker build --build-context wheels=<app-dir>/.wheels -t <image> <app-dir>
set -euo pipefail
APP="${1:?app dir}"; ARCH="${2:-aarch64}"
cd "$APP"
OUT=.wheels
rm -rf "$OUT"; mkdir -p "$OUT"
uv export --frozen --no-dev --no-emit-project --no-header --format requirements-txt -o "$OUT/requirements.txt" >/dev/null
uv run --no-project --python 3.12 --with pip==25.2 python -m pip download --quiet \
  --only-binary=:all: --implementation cp --python-version 3.12 --abi cp312 --abi abi3 --abi none \
  --platform manylinux_2_28_"$ARCH" --platform manylinux_2_17_"$ARCH" --platform manylinux2014_"$ARCH" --platform any \
  --require-hashes -r "$OUT/requirements.txt" -d "$OUT"
echo "$(ls "$OUT"/*.whl | wc -l | tr -d ' ') wheels in $APP/$OUT"
