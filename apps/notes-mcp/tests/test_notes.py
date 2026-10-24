import asyncio

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from notes_mcp.app import create_app

TOKEN = "t" * 40
BASE = "http://localhost:8000"


@pytest.fixture
async def app(tmp_path):
    application = create_app(token=TOKEN, db_path=str(tmp_path / "notes.db"), allowed_hosts=["localhost:*"])
    started, stop = asyncio.Event(), asyncio.Event()

    async def life():
        async with application.router.lifespan_context(application):
            started.set()
            await stop.wait()

    task = asyncio.create_task(life())
    await started.wait()
    yield application
    stop.set()
    await task


async def test_requires_token(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
        for h in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}):
            r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=h)
            assert r.status_code == 401
        assert (await c.get("/healthz")).status_code == 200


async def test_crud_and_validation(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE,
                                 headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        async with streamable_http_client(f"{BASE}/mcp", http_client=client) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = {t.name: t for t in (await s.list_tools()).tools}
                assert set(tools) == {"create_note", "list_notes", "get_note", "delete_note"}
                assert tools["list_notes"].annotations.readOnlyHint is True
                assert tools["delete_note"].annotations.destructiveHint is True

                n = (await s.call_tool("create_note", {"title": "Hello", "body": "100% done_ok"})).structuredContent
                await s.call_tool("create_note", {"title": "Other", "body": "x"})
                assert (await s.call_tool("get_note", {"note_id": n["id"]})).structuredContent["body"] == "100% done_ok"
                lst = (await s.call_tool("list_notes", {"query": "100%"})).structuredContent
                assert lst["total"] == 1 and lst["notes"][0]["id"] == n["id"]
                assert (await s.call_tool("list_notes", {"query": "_"})).structuredContent["total"] == 1
                assert (await s.call_tool("list_notes", {})).structuredContent["total"] == 2

                for bad in [("list_notes", {"limit": 51}), ("create_note", {"title": "", "body": "x"}),
                            ("create_note", {"title": "a\x00", "body": "x"}), ("get_note", {"note_id": 0}),
                            ("get_note", {"note_id": 99999})]:
                    assert (await s.call_tool(*bad)).isError, bad
                assert (await s.call_tool("delete_note", {"note_id": n["id"]})).structuredContent["deleted"]
                assert (await s.call_tool("get_note", {"note_id": n["id"]})).isError


def test_refuses_short_token(tmp_path):
    with pytest.raises(SystemExit):
        create_app(token="short", db_path=str(tmp_path / "n.db"))
