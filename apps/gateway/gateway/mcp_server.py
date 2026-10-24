"""The gateway's own MCP server: aggregated, namespaced tools over Streamable HTTP."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from mcp import types
from mcp.server.lowlevel import Server

from .auth import PRINCIPAL_KEY
from .security import describe_exception
from .store import Principal, Store
from .upstreams import UpstreamManager, monotonic_ms

log = logging.getLogger(__name__)


def _principal(server: Server) -> Principal:
    request = server.request_context.request
    principal = getattr(request, "scope", {}).get("state", {}).get(PRINCIPAL_KEY) if request is not None else None
    if not isinstance(principal, Principal):  # middleware guarantees this; fail closed otherwise
        raise PermissionError("unauthenticated")
    return principal


def build_server(store: Store, manager: UpstreamManager, admin_only: Callable[[], Any]) -> Server:
    server: Server = Server(
        "urumi-gateway",
        instructions="Aggregated tools from the team's MCP servers. Tool names are '<server>__<tool>'.",
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        principal = _principal(server)
        return manager.visible_tools(principal, await admin_only())

    # Input is validated against the upstream's schema before forwarding.
    @server.call_tool(validate_input=True)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        principal = _principal(server)
        start = time.monotonic()
        slug: str | None = None
        audit_error: str | None
        try:
            result, slug, audit_error = await manager.call(principal, name, arguments or {})
        except Exception as exc:  # noqa: BLE001 - never leak internals, always audit
            log.exception("tool call %s failed unexpectedly", name)
            result = types.CallToolResult(content=[types.TextContent(type="text", text="Internal gateway error.")], isError=True)
            audit_error = describe_exception(exc)
        try:
            await store.audit(
                event="tool_call", principal=principal, success=audit_error is None,
                upstream_slug=slug, tool_name=name[:160], duration_ms=monotonic_ms(start), error=audit_error,
                # Argument names only; values may contain personal or sensitive data.
                detail={"argument_names": sorted(arguments)[:50]} if arguments else None,
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to write audit record for %s", name)
        log.info("tool_call user=%s tool=%s ok=%s ms=%s", principal.user_id, name, audit_error is None, monotonic_ms(start))
        return result

    return server
