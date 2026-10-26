"""Real adapters against in-process fakes: MCP gateway client and OpenAI-compatible provider."""

import json

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from dashboard_bff.gateway_mcp import GatewayAuthError, McpGatewayClient
from dashboard_bff.llm import OpenAICompatibleProvider, ProviderError, provider_from_env

KEY = "urumi_0123456789ab_" + "K" * 43
PROVIDER_KEY = "sk-test-" + "p" * 32


def fake_gateway():
    mcp = FastMCP("gw", host="0.0.0.0", stateless_http=True, json_response=True)
    seen = {}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def notes__list_notes() -> dict:
        return {"notes": []}

    @mcp.tool()
    def notes__create_note(title: str, body: str) -> dict:
        return {"id": 1, "title": title}

    inner = mcp.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            seen["auth"] = auth
            if auth != f"Bearer {KEY}":
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b"{}"})
                return
        await inner(scope, receive, send)

    return mcp, app, seen


async def test_mcp_client_uses_session_key_and_reads_annotations():
    mcp, app, seen = fake_gateway()
    async with mcp.session_manager.run():
        client = McpGatewayClient("http://gateway.test:8080", transport=httpx.ASGITransport(app=app))
        tools = {t.name: t for t in await client.list_tools(KEY)}
        assert tools["notes__list_notes"].read_only_hint is True
        assert tools["notes__create_note"].read_only_hint is None
        assert tools["notes__create_note"].input_schema["required"] == ["title", "body"]
        out = await client.call_tool(KEY, "notes__create_note", {"title": "t", "body": "b"})
        assert not out.is_error and json.loads(out.text) == {"id": 1, "title": "t"}
        assert seen["auth"] == f"Bearer {KEY}"


async def test_mcp_client_maps_401_to_auth_error():
    mcp, app, _ = fake_gateway()
    async with mcp.session_manager.run():
        client = McpGatewayClient("http://gateway.test:8080", transport=httpx.ASGITransport(app=app))
        with pytest.raises(GatewayAuthError):
            await client.list_tools("urumi_000000000000_" + "x" * 43)


def provider(handler):
    return OpenAICompatibleProvider(base_url="https://llm.test/v1", model="m", api_key=PROVIDER_KEY, timeout=1,
                                    transport=httpx.MockTransport(handler))


async def test_provider_request_and_tool_call_parsing():
    captured = {}

    def handler(req: httpx.Request):
        captured["auth"] = req.headers["authorization"]
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "notes__list_notes", "arguments": "{}"}}]}}]})

    tools = [{"type": "function", "function": {"name": "notes__list_notes", "parameters": {"type": "object"}}}]
    r = await provider(handler).complete([{"role": "user", "content": "hi"}], tools)
    assert r.tool_calls[0].name == "notes__list_notes" and r.tool_calls[0].arguments == "{}"
    assert captured["auth"] == f"Bearer {PROVIDER_KEY}" and captured["body"]["tools"] == tools
    assert captured["body"]["model"] == "m" and captured["body"]["tool_choice"] == "auto"


@pytest.mark.parametrize("response,kind", [
    (httpx.Response(429, headers={"retry-after": "3"}), "rate_limited"),
    (httpx.Response(401, json={"error": {"message": f"bad key {PROVIDER_KEY}"}}), "auth"),
    (httpx.Response(400, json={"error": {"message": "This model's maximum context length is 8192 tokens"}}), "context_length"),
    (httpx.Response(400, json={"error": {"message": f"invalid tool schema; key={PROVIDER_KEY}"}}), "bad_request"),
    (httpx.Response(503), "unavailable"),
    (httpx.Response(200, json={"nope": True}), "malformed"),
    (httpx.Response(200, text="<html>"), "malformed"),
])
async def test_provider_error_mapping(response, kind):
    with pytest.raises(ProviderError) as exc:
        await provider(lambda req: response).complete([{"role": "user", "content": "x"}], [])
    assert exc.value.kind == kind and PROVIDER_KEY not in exc.value.message
    if kind == "rate_limited":
        assert exc.value.retry_after == 3


@pytest.mark.parametrize("headers,expected", [
    ({"retry-after": "7"}, 7.0),
    ({"x-ratelimit-reset-tokens": "38.362s", "x-ratelimit-reset-requests": "2m0.5s"}, 120.5),
    ({"x-ratelimit-reset-tokens": "250ms"}, 0.25),
    ({}, None),
])
async def test_rate_limit_retry_hint_parsing(headers, expected):
    with pytest.raises(ProviderError) as exc:
        await provider(lambda req: httpx.Response(429, headers=headers)).complete([{"role": "user", "content": "x"}], [])
    assert exc.value.kind == "rate_limited" and exc.value.retry_after == expected


async def test_provider_timeout():
    def handler(req):
        # MockTransport has no socket, so raise what httpx raises on a real read timeout.
        raise httpx.ReadTimeout("timed out", request=req)

    with pytest.raises(ProviderError) as exc:
        await provider(handler).complete([{"role": "user", "content": "x"}], [])
    assert exc.value.kind == "timeout"


def test_provider_from_env():
    assert provider_from_env({}) is None
    assert provider_from_env({"CHAT_PROVIDER_BASE_URL": "https://x/v1"}) is None
    p = provider_from_env({"CHAT_PROVIDER_BASE_URL": "https://x/v1", "CHAT_MODEL": "m", "CHAT_PROVIDER_API_KEY": PROVIDER_KEY})
    assert p is not None and p.model == "m" and PROVIDER_KEY not in repr(vars(p).get("_url"))
    with pytest.raises(SystemExit):
        provider_from_env({"CHAT_PROVIDER_BASE_URL": "http://evil.example/v1", "CHAT_MODEL": "m"})
