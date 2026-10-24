"""The real MCP client connector against in-process FastMCP upstreams."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp.server.fastmcp import Context, FastMCP

from gateway.app import create_app
from gateway.upstreams import McpConnector, UpstreamError, UpstreamTarget

from .conftest import add_upstream, mcp_session

URL = "http://notes.urumi.svc.cluster.local:8000/mcp"


def upstream_app():
    up = FastMCP("upstream", host="0.0.0.0", stateless_http=True, json_response=True)

    @up.tool()
    def whoami(ctx: Context) -> dict:
        """Echo the headers the upstream received (test only)."""
        req = ctx.request_context.request
        return {"authorization": req.headers.get("authorization"), "x_api_key": req.headers.get("x-api-key"),
                "all_headers": sorted(req.headers.keys())}

    @up.tool()
    async def slow(seconds: float) -> str:
        await asyncio.sleep(seconds)
        return "done"

    @up.tool()
    def boom() -> str:
        raise ValueError("internal failure with token=abc123")

    return up


@asynccontextmanager
async def running(up: FastMCP):
    app = up.streamable_http_app()
    async with up.session_manager.run():
        yield httpx.ASGITransport(app=app)


def payload(r):
    return json.loads(r.content[0].text)


def target(ref=None):
    return UpstreamTarget("id1", "notes", URL, ref)


async def test_sends_server_side_credential_only(settings, fake_dns):
    async with running(upstream_app()) as transport:
        c = McpConnector(settings, transport=transport)
        tools = {t.name for t in await c.list_tools(target("notes-token"))}
        assert tools == {"whoami", "slow", "boom"}
        r = await c.call_tool(target("notes-token"), "whoami", {})
        assert payload(r)["authorization"] == "Bearer notes-secret-token-value"
        r = await c.call_tool(target(None), "whoami", {})
        assert payload(r)["authorization"] is None


async def test_member_gateway_key_is_never_forwarded(settings, connector, fake_dns):
    async with running(upstream_app()) as transport:
        app = create_app(settings, connector=McpConnector(settings, transport=transport), background_discovery=False)
        started, stop = asyncio.Event(), asyncio.Event()

        async def life():
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()

        task = asyncio.create_task(life())
        await started.wait()
        try:
            store = app.state.store
            admin = await store.create_user("root", "admin")
            akey, _ = await store.create_key(admin["id"], "t")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080") as http:
                await add_upstream(http, {"h": {"Authorization": f"Bearer {akey.raw}"}}, "notes", credential_ref="notes-token")
            async with mcp_session(app, akey.raw) as s:
                r = await s.call_tool("notes__whoami", {})
            seen = payload(r)
            assert seen["authorization"] == "Bearer notes-secret-token-value"
            assert akey.raw not in str(seen) and "urumi_" not in str(seen)
            assert seen["x_api_key"] is None
        finally:
            stop.set()
            await task


async def test_call_timeout(settings, fake_dns):
    async with running(upstream_app()) as transport:
        c = McpConnector(settings, transport=transport)  # call_timeout=0.5
        with pytest.raises(UpstreamError) as exc:
            await c.call_tool(target(), "slow", {"seconds": 5})
        assert exc.value.category == "timeout"


async def test_upstream_tool_exception_is_a_tool_error_not_a_crash(settings, fake_dns):
    async with running(upstream_app()) as transport:
        r = await McpConnector(settings, transport=transport).call_tool(target(), "boom", {})
        assert r.isError


async def test_missing_credential_reference(settings, fake_dns):
    async with running(upstream_app()) as transport:
        with pytest.raises(UpstreamError) as exc:
            await McpConnector(settings, transport=transport).list_tools(target("does-not-exist"))
        assert exc.value.category == "config" and "not available" in exc.value.detail


async def test_connection_refused_is_contained(settings, fake_dns):
    fake_dns["notes.urumi.svc.cluster.local"] = "10.43.255.254"

    class Refuse(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused")

    with pytest.raises(UpstreamError) as exc:
        await McpConnector(settings, transport=Refuse()).list_tools(target())
    assert exc.value.category in ("connection", "protocol")


async def test_ssrf_rechecked_at_connect_time(settings, fake_dns):
    fake_dns["notes.urumi.svc.cluster.local"] = "169.254.169.254"  # DNS changed after registration
    async with running(upstream_app()) as transport:
        with pytest.raises(UpstreamError) as exc:
            await McpConnector(settings, transport=transport).list_tools(target())
        assert exc.value.category == "policy"


async def test_redirects_to_other_origins_are_not_followed(settings, fake_dns):
    hits = []

    class Redirect(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            hits.append(str(request.url))
            return httpx.Response(307, headers={"Location": "http://169.254.169.254/latest/meta-data"})

    with pytest.raises(UpstreamError):
        await McpConnector(settings, transport=Redirect()).list_tools(target("notes-token"))
    assert all("169.254" not in h for h in hits)
