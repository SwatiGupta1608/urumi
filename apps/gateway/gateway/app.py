"""FastAPI application: MCP endpoint (/mcp), REST API (/api/v1), health probes.

Run with:  uvicorn --factory gateway.app:create_app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import anyio
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.routing import Route

from . import api
from .auth import McpAuthMiddleware
from .config import Settings
from .db import init_schema, make_engine
from .mcp_server import build_server
from .store import Store
from .upstreams import Connector, McpConnector, UpstreamManager

log = logging.getLogger("gateway")


def create_app(settings: Settings | None = None, *, connector: Connector | None = None,
               engine: AsyncEngine | None = None, background_discovery: bool = True) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = settings or Settings.from_env()
    engine = engine or make_engine(settings.database_url)
    store = Store(engine, settings.key_pepper)
    manager = UpstreamManager(store, connector or McpConnector(settings), settings)

    async def admin_only() -> dict[str, set[str]]:
        return {u["slug"]: set(u["admin_only_tools"] or []) for u in await store.list_upstreams()}

    server = build_server(store, manager, admin_only)
    sessions = StreamableHTTPSessionManager(
        app=server, stateless=True, json_response=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=list(settings.allowed_origins),
        ),
    )

    async def mcp_asgi(scope, receive, send):
        await sessions.handle_request(scope, receive, send)

    async def discovery_loop() -> None:
        while True:
            try:
                results = await manager.discover_all()
                log.info("discovery: %s", ", ".join(f"{r.slug}={'ok' if r.ok else 'fail'}({r.tool_count})" for r in results) or "no upstreams")
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("discovery loop iteration failed")
            await anyio.sleep(settings.discovery_interval)

    async def init_schema_with_retry(deadline_s: float = 120.0) -> None:
        # PostgreSQL may still be starting (or its image pulling); retry instead
        # of crash-looping. /readyz stays 503 until the database answers.
        delay, waited = 1.0, 0.0
        while True:
            try:
                await init_schema(engine)
                return
            except Exception as exc:  # noqa: BLE001
                if waited >= deadline_s:
                    raise
                log.warning("database not ready (%s); retrying in %.0fs", type(exc).__name__, delay)
                await anyio.sleep(delay)
                waited += delay
                delay = min(delay * 2, 15.0)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await init_schema_with_retry()
        async with sessions.run():
            async with anyio.create_task_group() as tg:
                if background_discovery:
                    tg.start_soon(discovery_loop)
                try:
                    yield
                finally:
                    tg.cancel_scope.cancel()
        await engine.dispose()

    app = FastAPI(title="Urumi MCP Gateway", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/api/v1/openapi.json")
    app.state.settings = settings
    app.state.store = store
    app.state.manager = manager
    app.state.admin_only = admin_only
    app.include_router(api.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        # Ready when the database answers; upstream health is reported per upstream.
        try:
            with anyio.fail_after(3):
                async with engine.connect() as conn:
                    await conn.execute(sa.text("SELECT 1"))
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "database unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    # Exact route (no trailing-slash redirect), wrapped in API-key authentication.
    app.router.routes.append(Route("/mcp", endpoint=McpAuthMiddleware(mcp_asgi, store), methods=["GET", "POST", "DELETE"]))
    return app
