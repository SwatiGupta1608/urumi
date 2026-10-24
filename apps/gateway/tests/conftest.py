import ipaddress
import socket
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx
import pytest
from mcp import types

from gateway.app import create_app
from gateway.config import Settings
from gateway.upstreams import UpstreamError, UpstreamTarget

PEPPER = "p" * 40
BASE = "http://localhost:8080"


class FakeConnector:
    """In-memory upstreams: slug -> list of tools, plus per-slug behaviour."""

    def __init__(self):
        self.tools: dict[str, list[types.Tool]] = {}
        self.list_behaviour: dict[str, Any] = {}   # slug -> Exception | "hang"
        self.call_behaviour: dict[str, Any] = {}   # slug -> Exception | "hang" | CallToolResult
        self.calls: list[tuple[UpstreamTarget, str, dict]] = []

    @staticmethod
    def slug_of(target: UpstreamTarget) -> str:
        return target.slug

    async def list_tools(self, target):
        b = self.list_behaviour.get(target.slug)
        if b == "hang":
            await anyio.sleep(60)
        if isinstance(b, Exception):
            raise b
        return list(self.tools.get(target.slug, []))

    async def call_tool(self, target, name, arguments):
        self.calls.append((target, name, arguments))
        b = self.call_behaviour.get(target.slug)
        if b == "hang":
            await anyio.sleep(60)
        if isinstance(b, Exception):
            raise b
        if isinstance(b, types.CallToolResult):
            return b
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"{target.slug}:{name}")],
                                    structuredContent={"upstream": target.slug, "tool": name, "args": arguments})


def tool(name: str, **schema_props) -> types.Tool:
    return types.Tool(name=name, description=f"{name} tool",
                      inputSchema={"type": "object", "properties": schema_props or {}, "additionalProperties": True})


@pytest.fixture
def fake_dns(monkeypatch):
    """Resolve *.svc.cluster.local to an in-cluster service IP; allow overrides per host."""
    table: dict[str, str] = {}

    async def getaddrinfo(host, port, *args, **kwargs):
        ip = table.get(host)
        if ip is None:
            if host.endswith(".svc.cluster.local"):
                ip = "10.43.0.10"
            else:
                raise socket.gaierror("not found")
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(anyio, "getaddrinfo", getaddrinfo)
    return table


@pytest.fixture
def settings(tmp_path):
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "notes-token").write_text("notes-secret-token-value\n")
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'gw.db'}",
        key_pepper=PEPPER.encode(),
        credentials_dir=str(creds),
        upstream_allowed_hosts=("*.svc.cluster.local",),
        upstream_allowed_cidrs=(ipaddress.ip_network("10.42.0.0/16"), ipaddress.ip_network("10.43.0.0/16")),
        discovery_timeout=0.5, call_timeout=0.5, connect_timeout=0.5,
        allowed_hosts=("localhost:*",),
    )


@pytest.fixture
def connector():
    return FakeConnector()


@pytest.fixture
async def app(settings, connector, fake_dns):
    import asyncio

    application = create_app(settings, connector=connector, background_discovery=False)
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        # Enter and exit the lifespan in one task (as uvicorn does); its task
        # groups must not be exited from a different task.
        async with application.router.lifespan_context(application):
            started.set()
            await stop.wait()

    task = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 10)
    yield application
    stop.set()
    await asyncio.wait_for(task, 10)


@pytest.fixture
async def http(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
        yield c


@pytest.fixture
async def admin(app):
    store = app.state.store
    user = await store.create_user("root", "admin")
    key, _ = await store.create_key(user["id"], "test")
    return {"id": user["id"], "key": key.raw, "h": {"Authorization": f"Bearer {key.raw}"}}


@pytest.fixture
async def member(app):
    store = app.state.store
    user = await store.create_user("alice", "member")
    key, _ = await store.create_key(user["id"], "test")
    return {"id": user["id"], "key": key.raw, "h": {"Authorization": f"Bearer {key.raw}"}}


async def add_upstream(http, admin, slug, **over):
    body = {"slug": slug, "display_name": slug.title(), "url": f"http://{slug}.urumi.svc.cluster.local:8000/mcp"}
    body.update(over)
    r = await http.post("/api/v1/upstreams", json=body, headers=admin["h"])
    assert r.status_code == 201, r.text
    return r.json()


@asynccontextmanager
async def mcp_session(app, key: str | None):
    """MCP client session to the gateway over Streamable HTTP (in-process)."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, headers=headers) as client:
        async with streamable_http_client(f"{BASE}/mcp", http_client=client) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                yield s
