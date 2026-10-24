"""MCP-level behaviour through the gateway endpoint (real MCP client, in-process)."""

import sqlalchemy as sa
from mcp import types

from gateway import db
from gateway.upstreams import UpstreamError

from .conftest import add_upstream, mcp_session, tool


async def audit_rows(app, event="tool_call"):
    async with app.state.store.engine.connect() as conn:
        rows = (await conn.execute(sa.select(db.audit_log).where(db.audit_log.c.event == event)
                                   .order_by(db.audit_log.c.created_at))).all()
    return [dict(r._mapping) for r in rows]


async def setup_three(http, admin, connector):
    connector.tools = {
        "shop": [tool("list_products"), tool("update_order_status", order_id={"type": "integer"})],
        "notes": [tool("create_note", title={"type": "string"})],
        "github": [tool("get_me")],
    }
    await add_upstream(http, admin, "shop", admin_only_tools=["update_order_status"])
    await add_upstream(http, admin, "notes", credential_ref="notes-token")
    await add_upstream(http, admin, "github")


async def test_list_and_call_through_gateway(app, http, admin, member, connector):
    await setup_three(http, admin, connector)
    async with mcp_session(app, member["key"]) as s:
        names = sorted(t.name for t in (await s.list_tools()).tools)
        assert names == ["github__get_me", "notes__create_note", "shop__list_products"]
        for name in names:
            r = await s.call_tool(name, {})
            assert not r.isError, r
        r = await s.call_tool("notes__create_note", {"title": "hello"})
        assert r.structuredContent == {"upstream": "notes", "tool": "create_note", "args": {"title": "hello"}}
    # Upstream received the original tool name and the server-side target, nothing else.
    targets = {(t.slug, name) for t, name, _ in connector.calls}
    assert ("notes", "create_note") in targets and ("shop", "list_products") in targets


async def test_admin_only_tool_hidden_and_rejected_for_member(app, http, admin, member, connector):
    await setup_three(http, admin, connector)
    async with mcp_session(app, member["key"]) as s:
        r = await s.call_tool("shop__update_order_status", {"order_id": 17})
        assert r.isError and "admin role" in r.content[0].text
    async with mcp_session(app, admin["key"]) as s:
        assert "shop__update_order_status" in {t.name for t in (await s.list_tools()).tools}
        assert not (await s.call_tool("shop__update_order_status", {"order_id": 17})).isError
    assert sum(1 for _, n, _ in connector.calls if n == "update_order_status") == 1


async def test_disabled_upstream_is_rejected_even_with_stale_registration(app, http, admin, member, connector):
    await setup_three(http, admin, connector)
    async with mcp_session(app, member["key"]) as s:
        await s.list_tools()
        # Disable directly in the DB (as another replica would) without touching this process's registry.
        await app.state.store.update_upstream("notes", enabled=False)
        assert app.state.manager.registered("notes__create_note") is not None
        r = await s.call_tool("notes__create_note", {"title": "x"})
        assert r.isError and "disabled or removed" in r.content[0].text
        assert app.state.manager.registered("notes__create_note") is None
        assert "notes__create_note" not in {t.name for t in (await s.list_tools()).tools}
    assert not any(t.slug == "notes" for t, _, _ in connector.calls)


async def test_deleted_and_recreated_upstream_does_not_reuse_stale_registration(app, http, admin, connector):
    await setup_three(http, admin, connector)
    store = app.state.store
    await store.delete_upstream("notes")
    await store.create_upstream(slug="notes", display_name="New", url="http://other.ns.svc.cluster.local:8000/mcp",
                                credential_ref=None, enabled=True, admin_only_tools=[])
    async with mcp_session(app, admin["key"]) as s:
        r = await s.call_tool("notes__create_note", {"title": "x"})
        assert r.isError


async def test_unknown_tool_and_input_validation(app, http, admin, member, connector):
    await setup_three(http, admin, connector)
    async with mcp_session(app, member["key"]) as s:
        assert (await s.call_tool("nope__tool", {})).isError
        await s.list_tools()
        r = await s.call_tool("notes__create_note", {"title": 123})  # schema says string
        assert r.isError
    assert not any(n == "create_note" for _, n, _ in connector.calls)


async def test_upstream_timeout_and_errors_isolated_and_sanitized(app, http, admin, member, connector):
    await setup_three(http, admin, connector)
    connector.call_behaviour = {
        "notes": "hang",
        "github": UpstreamError("protocol", "HTTP 401 Authorization: Bearer ghp_" + "z" * 36),
    }
    async with mcp_session(app, member["key"]) as s:
        r = await s.call_tool("notes__create_note", {"title": "x"})
        assert r.isError and "did not respond in time" in r.content[0].text
        r = await s.call_tool("github__get_me", {})
        assert r.isError and "ghp_" not in r.content[0].text and "invalid response" in r.content[0].text
        assert not (await s.call_tool("shop__list_products", {})).isError  # healthy upstream unaffected
    rows = await audit_rows(app)
    gh = [r for r in rows if r["tool_name"] == "github__get_me"][0]
    assert not gh["success"] and "ghp_" + "z" * 36 not in gh["error"]
    assert [r for r in rows if r["tool_name"] == "notes__create_note"][0]["error"].startswith("timeout")


async def test_audit_attribution_success_and_failure(app, http, admin, member, connector):
    await setup_three(http, admin, connector)
    connector.call_behaviour["github"] = types.CallToolResult(
        content=[types.TextContent(type="text", text="Bad credentials")], isError=True)
    async with mcp_session(app, member["key"]) as s:
        # A client-supplied user_id is just a tool argument: never used for attribution.
        await s.call_tool("shop__list_products", {"user_id": admin["id"], "secret_note": "do not log me"})
        await s.call_tool("github__get_me", {})
        await s.call_tool("shop__update_order_status", {"order_id": 1})
    rows = await audit_rows(app)
    assert len(rows) == 3 and all(r["user_id"] == member["id"] for r in rows)
    ok, gh, denied = rows
    assert ok["success"] and ok["upstream_slug"] == "shop" and ok["tool_name"] == "shop__list_products"
    assert ok["duration_ms"] is not None and ok["detail"] == {"argument_names": ["secret_note", "user_id"]}
    assert "do not log me" not in str(rows)
    assert not gh["success"] and gh["error"].startswith("tool error: Bad credentials")
    assert not denied["success"] and "admin-only" in denied["error"]
    # Admin can read audit; members cannot.
    r = await http.get("/api/v1/audit", params={"user_id": member["id"]}, headers=admin["h"])
    assert len([a for a in r.json() if a["event"] == "tool_call"]) == 3
    assert (await http.get("/api/v1/audit", headers=member["h"])).status_code == 403


async def test_admin_actions_are_audited(app, http, admin, member, connector):
    await setup_three(http, admin, connector)
    await http.post(f"/api/v1/users/{member['id']}/keys", json={"name": "x"}, headers=admin["h"])
    await http.patch("/api/v1/upstreams/notes", json={"enabled": False}, headers=admin["h"])
    events = {r["event"] for r in (await http.get("/api/v1/audit", headers=admin["h"])).json()}
    assert {"admin.upstream.create", "admin.key.create", "admin.upstream.update"} <= events
    assert all(r["user_id"] == admin["id"] for r in (await http.get("/api/v1/audit", headers=admin["h"])).json())


async def test_unauthenticated_mcp_rejected(app):
    import httpx
    import pytest
    with pytest.raises(Exception):
        async with mcp_session(app, None):
            pass
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080") as c:
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={"Accept": "application/json, text/event-stream"})
        assert r.status_code == 401


async def test_disallowed_host_header_rejected(app, member):
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://evil.example:8080") as c:
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={**member["h"], "Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
        assert r.status_code == 421
