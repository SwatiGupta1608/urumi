"""MCP client for the gateway's Streamable HTTP endpoint (POST /mcp).

Every request authenticates as the signed-in user with that user's own
gateway key (from the server-side session). The gateway decides which tools
the user may see and call, and attributes each call in its audit log.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

import anyio
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

log = logging.getLogger(__name__)


class GatewayAuthError(Exception):
    """The gateway rejected the user's key (revoked, user disabled)."""


class GatewayUnavailable(Exception):
    """The gateway could not be reached or did not answer in time."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str | None
    description: str | None
    input_schema: dict[str, Any]
    read_only_hint: bool | None  # from the upstream's MCP annotations, passed through by the gateway
    destructive_hint: bool | None


@dataclass(frozen=True)
class ToolOutcome:
    is_error: bool
    text: str
    structured: dict[str, Any] | None


class GatewayTools(Protocol):
    async def list_tools(self, key: str) -> list[ToolSpec]: ...
    async def call_tool(self, key: str, name: str, arguments: dict[str, Any]) -> ToolOutcome: ...


def _status_in(exc: BaseException) -> int | None:
    """Find an HTTP status in an (exception-group) chain."""
    stack = [exc]
    seen = 0
    while stack and seen < 50:
        e = stack.pop()
        seen += 1
        if isinstance(e, httpx.HTTPStatusError):
            return e.response.status_code
        if isinstance(e, BaseExceptionGroup):
            stack.extend(e.exceptions)
        for nxt in (e.__cause__, e.__context__):
            if nxt is not None:
                stack.append(nxt)
    return None


class McpGatewayClient:
    def __init__(self, gateway_url: str, *, list_timeout: float = 10.0, call_timeout: float = 45.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._url = gateway_url.rstrip("/") + "/mcp"
        self._list_timeout = list_timeout
        self._call_timeout = call_timeout
        self._transport = transport  # tests only

    async def _with_session(self, key: str, timeout: float, op):
        headers = {"Authorization": f"Bearer {key}", "User-Agent": "urumi-dashboard-chat/0.1"}
        try:
            with anyio.fail_after(timeout):
                async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(connect=3, read=timeout, write=10, pool=3),
                                             follow_redirects=False, trust_env=False, transport=self._transport) as http:
                    async with streamable_http_client(self._url, http_client=http, terminate_on_close=False) as (r, w, _):
                        async with ClientSession(r, w, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                            await session.initialize()
                            return await op(session)
        except TimeoutError:
            raise GatewayUnavailable(f"the gateway did not respond within {timeout:g}s") from None
        except Exception as exc:  # noqa: BLE001 - classify everything; never leak the key
            status = _status_in(exc)
            if status == 401:
                raise GatewayAuthError("the gateway rejected this session's API key") from None
            log.warning("gateway MCP request failed (%s, status=%s)", type(exc).__name__, status)
            raise GatewayUnavailable("the gateway MCP endpoint is unavailable" + (f" (HTTP {status})" if status else "")) from None

    async def list_tools(self, key: str) -> list[ToolSpec]:
        async def op(session: ClientSession) -> list[ToolSpec]:
            out: list[ToolSpec] = []
            cursor = None
            for _ in range(10):
                page = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
                for t in page.tools:
                    ann = t.annotations
                    out.append(ToolSpec(
                        name=t.name, title=t.title, description=t.description,
                        input_schema=t.inputSchema or {"type": "object"},
                        read_only_hint=getattr(ann, "readOnlyHint", None) if ann else None,
                        destructive_hint=getattr(ann, "destructiveHint", None) if ann else None,
                    ))
                cursor = page.nextCursor
                if not cursor:
                    break
            return out

        return await self._with_session(key, self._list_timeout, op)

    async def call_tool(self, key: str, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        async def op(session: ClientSession) -> ToolOutcome:
            r = await session.call_tool(name, arguments)
            text = " ".join(c.text for c in r.content if isinstance(c, types.TextContent))
            return ToolOutcome(is_error=bool(r.isError), text=text, structured=r.structuredContent)

        return await self._with_session(key, self._call_timeout, op)
