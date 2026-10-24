# Build from apps/notes-mcp:  docker build -t urumi/notes-mcp:0.1.0 .
FROM ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 AS uv

# Optional pre-downloaded wheels (scripts/prefetch-wheels.sh), passed with
#   --build-context wheels=<app>/.wheels
# Empty by default, in which case dependencies are downloaded normally.
FROM scratch AS wheels

FROM python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
# Build-time only: tolerate slow registry networks.
ARG UV_HTTP_TIMEOUT=300
ARG UV_CONCURRENT_DOWNLOADS=4
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=bind,from=wheels,target=/wheels \
    if [ -f /wheels/requirements.txt ]; then \
      uv export --frozen --no-dev --no-emit-project --no-header --format requirements-txt -o /tmp/req.txt >/dev/null && \
      cmp -s /tmp/req.txt /wheels/requirements.txt || { echo "wheelhouse does not match uv.lock" >&2; exit 1; }; \
      uv venv /app/.venv && \
      uv pip install --python /app/.venv/bin/python --no-index --find-links /wheels --require-hashes -r /tmp/req.txt; \
    else \
      uv sync --frozen --no-dev --no-install-project; \
    fi

FROM python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY notes_mcp ./notes_mcp
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["uvicorn", "--factory", "notes_mcp.app:create_app", "--host", "0.0.0.0", "--no-server-header", "--timeout-graceful-shutdown", "10"]
CMD ["--port", "8000"]
