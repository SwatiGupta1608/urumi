"""Upstream MCP connections, tool discovery, namespaced registry and invocation.

Connection model: every discovery or tool call opens a short-lived Streamable
HTTP session (initialize -> request -> close) bounded by timeouts. Upstreams
are stateless MCP servers, so there is no long-lived session state to keep
consistent, failures are isolated per call, and shutdown has nothing to drain.

Naming: a tool `t` of upstream `s` is exposed as `s__t`. Slugs cannot contain
"_", so the first "__" always separates slug and tool. Duplicate or invalid
upstream tool names are skipped (and reported), never overwritten.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

import anyio
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from .config import Settings
from .security import describe_exception, sanitize
from .ssrf import UnsafeURL, validate_url
from .store import Principal, Store

log = logging.getLogger(__name__)

SEPARATOR = "__"
SLUG_RE = re.compile(r"^[a-z][a-z0-9]{0,15}(-[a-z0-9]+)*$")  # no "_", so "__" is unambiguous
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
CREDENTIAL_REF_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_TOOLS_PER_UPSTREAM = 500
MAX_LIST_PAGES = 10
MAX_CONCURRENT_CALLS_PER_UPSTREAM = 16


class UpstreamError(Exception):
    """Credential-free failure talking to an upstream."""

    def __init__(self, category: str, detail: str):
        super().__init__(f"{category}: {detail}")
        self.category = category  # timeout | connection | protocol | config | policy
        self.detail = sanitize(detail)


@dataclass(frozen=True)
class UpstreamTarget:
    upstream_id: str
    slug: str
    url: str
    credential_ref: str | None


class Connector(Protocol):
    async def list_tools(self, target: UpstreamTarget) -> list[types.Tool]: ...
    async def call_tool(self, target: UpstreamTarget, name: str, arguments: dict[str, Any]) -> types.CallToolResult: ...


class McpConnector:
    """Real connector: MCP client over Streamable HTTP with server-side credentials only."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self._s = settings
        self._transport = transport  # tests only: in-process upstreams

    def _credential(self, ref: str | None) -> str | None:
        if not ref:
            return None
        if not CREDENTIAL_REF_RE.match(ref):
            raise UpstreamError("config", "invalid credential reference")
        path = os.path.join(self._s.credentials_dir, ref)
        try:
            with open(path, encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError:
            raise UpstreamError("config", f"credential reference '{ref}' is not available") from None
        if not value:
            raise UpstreamError("config", f"credential reference '{ref}' is empty")
        return value

    def _http(self, target: UpstreamTarget, read_timeout: float) -> httpx.AsyncClient:
        # A fresh client per session: only the gateway's own server-side
        # credential is attached; nothing from the member's request is copied.
        headers = {"User-Agent": "urumi-mcp-gateway/0.1"}
        token = self._credential(target.credential_ref)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(connect=self._s.connect_timeout, read=read_timeout,
                                  write=read_timeout, pool=self._s.connect_timeout),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=0),
            transport=self._transport,
        )

    async def _session(self, target: UpstreamTarget, timeout: float, op):
        try:
            await validate_url(target.url, self._s)  # re-check DNS at connect time
            with anyio.fail_after(timeout):
                async with self._http(target, timeout) as http:
                    async with streamable_http_client(target.url, http_client=http, terminate_on_close=False) as (r, w, _):
                        async with ClientSession(r, w, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                            await session.initialize()
                            return await op(session)
        except TimeoutError:
            raise UpstreamError("timeout", f"no response within {timeout:g}s") from None
        except UpstreamError:
            raise
        except UnsafeURL as exc:
            raise UpstreamError("policy", str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - every upstream failure must be contained
            raise UpstreamError(_categorize(exc), describe_exception(exc)) from None

    async def list_tools(self, target: UpstreamTarget) -> list[types.Tool]:
        async def op(session: ClientSession) -> list[types.Tool]:
            tools: list[types.Tool] = []
            cursor = None
            for _ in range(MAX_LIST_PAGES):
                page = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
                tools.extend(page.tools)
                cursor = page.nextCursor
                if not cursor or len(tools) >= MAX_TOOLS_PER_UPSTREAM:
                    break
            return tools[:MAX_TOOLS_PER_UPSTREAM]

        return await self._session(target, self._s.discovery_timeout, op)

    async def call_tool(self, target: UpstreamTarget, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        async def op(session: ClientSession) -> types.CallToolResult:
            return await session.call_tool(name, arguments)

        return await self._session(target, self._s.call_timeout, op)


def _categorize(exc: BaseException) -> str:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "timeout"
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, OSError)):
        return "connection"
    return "protocol"


@dataclass(frozen=True)
class RegisteredTool:
    name: str            # namespaced
    slug: str
    upstream_id: str
    upstream_tool: str   # original name
    tool: types.Tool     # definition exposed to clients (namespaced name)


@dataclass
class DiscoveryResult:
    slug: str
    ok: bool
    tool_count: int = 0
    error: str | None = None
    skipped: list[str] = field(default_factory=list)


class UpstreamManager:
    def __init__(self, store: Store, connector: Connector, settings: Settings):
        self._store = store
        self._connector = connector
        self._settings = settings
        self._tools: dict[str, RegisteredTool] = {}
        self._limits: dict[str, anyio.Semaphore] = {}
        self._lock = anyio.Lock()

    # ---------- registry ----------

    def _drop(self, slug: str) -> None:
        for name in [n for n, t in self._tools.items() if t.slug == slug]:
            del self._tools[name]

    async def remove(self, slug: str) -> None:
        async with self._lock:
            self._drop(slug)

    def visible_tools(self, principal: Principal, admin_only: dict[str, set[str]]) -> list[types.Tool]:
        """Tools a principal may see. `admin_only` maps slug -> restricted upstream tool names."""
        out = []
        for reg in sorted(self._tools.values(), key=lambda t: t.name):
            if not principal.is_admin and reg.upstream_tool in admin_only.get(reg.slug, set()):
                continue
            out.append(reg.tool)
        return out

    def registered(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    # ---------- discovery ----------

    async def discover(self, slug: str) -> DiscoveryResult:
        row = await self._store.get_upstream(slug)
        if row is None:
            await self.remove(slug)
            return DiscoveryResult(slug=slug, ok=False, error="upstream not registered")
        if not row["enabled"]:
            await self.remove(slug)
            return DiscoveryResult(slug=slug, ok=False, error="upstream is disabled")

        target = UpstreamTarget(row["id"], slug, row["url"], row["credential_ref"])
        try:
            # Outer bound in addition to the connector's own timeouts.
            with anyio.fail_after(self._settings.discovery_timeout + 1):
                raw_tools = await self._connector.list_tools(target)
        except TimeoutError:
            err = f"timeout: no response within {self._settings.discovery_timeout:g}s"
            await self._store.record_health(slug, ok=False, error=err, tool_count=None)
            return DiscoveryResult(slug=slug, ok=False, error=err)
        except UpstreamError as exc:
            # Keep the last good tool list (calls will fail fast with a clear
            # error) so a transient outage does not churn client tool lists.
            await self._store.record_health(slug, ok=False, error=f"{exc.category}: {exc.detail}", tool_count=None)
            log.warning("discovery %s failed: %s", slug, exc.category)
            return DiscoveryResult(slug=slug, ok=False, error=f"{exc.category}: {exc.detail}")

        names = [getattr(t, "name", None) for t in raw_tools]
        counts: dict[str, int] = {}
        for n in names:
            if isinstance(n, str):
                counts[n] = counts.get(n, 0) + 1
        entries: dict[str, RegisteredTool] = {}
        skipped: list[str] = []
        for tool, name in zip(raw_tools, names):
            if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
                skipped.append(f"invalid tool name {sanitize(str(name), 60)!r}")
                continue
            if counts[name] > 1:
                # Ambiguous: expose neither copy rather than let one overwrite the other.
                if f"duplicate tool name {name!r}" not in skipped:
                    skipped.append(f"duplicate tool name {name!r}")
                continue
            namespaced = f"{slug}{SEPARATOR}{name}"
            if len(namespaced) > 128:
                skipped.append(f"tool name too long when namespaced: {name!r}")
                continue
            exposed = tool.model_copy(update={"name": namespaced})
            entries[namespaced] = RegisteredTool(namespaced, slug, row["id"], name, exposed)

        async with self._lock:
            # Re-check: the upstream may have been disabled/removed while we were discovering.
            current = await self._store.get_upstream(slug)
            if current is None or not current["enabled"] or current["id"] != row["id"]:
                self._drop(slug)
                return DiscoveryResult(slug=slug, ok=False, error="upstream changed during discovery")
            collisions = [n for n in entries if n in self._tools and self._tools[n].slug != slug]
            for n in collisions:  # cannot happen with valid slugs; never overwrite another upstream's tool
                skipped.append(f"collision with existing tool {n!r}")
                entries.pop(n)
            self._drop(slug)
            self._tools.update(entries)

        error = ("; ".join(skipped))[:500] if skipped else None
        await self._store.record_health(slug, ok=True, error=error, tool_count=len(entries))
        return DiscoveryResult(slug=slug, ok=True, tool_count=len(entries), error=error, skipped=skipped)

    async def discover_all(self) -> list[DiscoveryResult]:
        rows = await self._store.list_upstreams()
        known = {r["slug"] for r in rows if r["enabled"]}
        async with self._lock:
            for slug in {t.slug for t in self._tools.values()} - known:
                self._drop(slug)
        results: list[DiscoveryResult] = []

        async def one(slug: str) -> None:
            try:
                results.append(await self.discover(slug))
            except Exception as exc:  # noqa: BLE001 - isolate any unexpected failure per upstream
                log.exception("discovery %s crashed", slug)
                results.append(DiscoveryResult(slug=slug, ok=False, error=describe_exception(exc)))

        async with anyio.create_task_group() as tg:
            for slug in sorted(known):
                tg.start_soon(one, slug)
        return results

    # ---------- invocation ----------

    async def call(self, principal: Principal, name: str, arguments: dict[str, Any]) -> tuple[types.CallToolResult, str | None, str | None]:
        """Invoke a namespaced tool. Returns (result, upstream_slug, audit_error)."""
        reg = self._tools.get(name)
        slug, _, tool = name.partition(SEPARATOR)
        if reg is None:
            return _error(f"Unknown tool '{sanitize(name, 160)}'."), (slug or None), "unknown tool"

        # Re-check current configuration: stale registrations must not grant access.
        row = await self._store.get_upstream(reg.slug)
        if row is None or row["id"] != reg.upstream_id or not row["enabled"]:
            await self.remove(reg.slug)
            return _error(f"Tool '{name}' is not available (upstream '{reg.slug}' is disabled or removed)."), reg.slug, "upstream disabled or removed"
        if not principal.is_admin and reg.upstream_tool in set(row["admin_only_tools"] or []):
            return _error(f"Tool '{name}' requires the admin role."), reg.slug, "forbidden: admin-only tool"

        target = UpstreamTarget(row["id"], reg.slug, row["url"], row["credential_ref"])
        sem = self._limits.setdefault(reg.slug, anyio.Semaphore(MAX_CONCURRENT_CALLS_PER_UPSTREAM))
        try:
            with anyio.fail_after(self._settings.call_timeout + 1):
                async with sem:
                    result = await self._connector.call_tool(target, reg.upstream_tool, arguments)
        except TimeoutError:
            return _error(f"Upstream '{reg.slug}' did not respond in time."), reg.slug, f"timeout: no response within {self._settings.call_timeout:g}s"
        except UpstreamError as exc:
            msg = {
                "timeout": f"Upstream '{reg.slug}' did not respond in time.",
                "connection": f"Upstream '{reg.slug}' is unreachable.",
                "policy": f"Upstream '{reg.slug}' is blocked by the gateway's network policy.",
                "config": f"Upstream '{reg.slug}' is misconfigured; ask an admin.",
            }.get(exc.category, f"Upstream '{reg.slug}' returned an invalid response.")
            return _error(msg), reg.slug, f"{exc.category}: {exc.detail}"
        if result.isError:
            text = " ".join(c.text for c in result.content if isinstance(c, types.TextContent))
            return result, reg.slug, f"tool error: {sanitize(text, 200)}"
        return result, reg.slug, None


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], isError=True)


def monotonic_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
