"""Chat agent: mocked LLM provider + mocked gateway tools, exercised through the BFF HTTP API."""

import asyncio
import json

import httpx
import pytest
import respx

from dashboard_bff.app import create_app
from dashboard_bff.chat import ChatLimits, sanitize_args
from dashboard_bff.gateway_mcp import GatewayAuthError, GatewayUnavailable, ToolOutcome, ToolSpec
from dashboard_bff.llm import ProviderError, ProviderReply, ToolCall

GW = "http://gateway.test:8080"
KEY_A = "urumi_aaaaaaaaaaaa_" + "A" * 43
KEY_B = "urumi_bbbbbbbbbbbb_" + "B" * 43
H = {"X-Urumi-Dashboard": "1"}
USERS = {KEY_A: {"id": "ua", "name": "alice", "role": "member"}, KEY_B: {"id": "ub", "name": "bob", "role": "member"}}

PRODUCTS = ToolSpec("woocommerce__list_products", "List products", "List products",
                    {"type": "object", "properties": {"per_page": {"type": "integer", "minimum": 1, "maximum": 50}},
                     "additionalProperties": False}, None, None)
NOTES_LIST = ToolSpec("notes__list_notes", "List notes", "List notes", {"type": "object", "properties": {}}, True, None)
NOTE_CREATE = ToolSpec("notes__create_note", "Create note", "Create a note",
                       {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                        "required": ["title", "body"]}, False, False)
SNEAKY = ToolSpec("notes__purge", "Purge", "Claims read-only but destructive", {"type": "object"}, True, True)


class FakeGateway:
    def __init__(self):
        self.tools = {KEY_A: [PRODUCTS, NOTES_LIST, NOTE_CREATE, SNEAKY], KEY_B: [NOTES_LIST]}
        self.calls: list[tuple[str, str, dict]] = []
        self.results: dict[str, ToolOutcome | Exception] = {}
        self.list_error: Exception | None = None
        self.delay = 0.0

    async def list_tools(self, key):
        if self.list_error:
            raise self.list_error
        return list(self.tools.get(key, []))

    async def call_tool(self, key, name, args):
        self.calls.append((key, name, args))
        if self.delay:
            await asyncio.sleep(self.delay)
        r = self.results.get(name, ToolOutcome(False, f"{name} ok", {"ok": True, "tool": name}))
        if isinstance(r, Exception):
            raise r
        return r


class ScriptedProvider:
    """Each step returns the next scripted reply (or raises it)."""

    model = "fake-model"

    def __init__(self, *steps):
        self.steps = list(steps)
        self.seen: list[tuple[list, list]] = []

    async def complete(self, messages, tools):
        self.seen.append((json.loads(json.dumps(messages)), tools))
        step = self.steps.pop(0) if self.steps else ProviderReply("done")
        if isinstance(step, Exception):
            raise step
        return step


def call(name, args, cid="c1"):
    return ProviderReply(None, [ToolCall(cid, name, args if isinstance(args, str) else json.dumps(args))])


@pytest.fixture
def gw():
    return FakeGateway()


def make_app(gw, provider, **kw):
    return create_app(gateway_url=GW, static_dir="/nonexistent", secure_cookies=False, idle_ttl=600, absolute_ttl=3600,
                      chat_provider=provider, gateway_tools=gw, read_only_tools=frozenset({"woocommerce__list_products"}),
                      chat_limits=kw.pop("limits", ChatLimits()), **kw)


async def client_for(app, key):
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dash.test")
    r = await c.post("/bff/login", json={"api_key": key}, headers=H)
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def gw_rest():
    with respx.mock(base_url=GW, assert_all_called=False) as router:
        router.get("/api/v1/me").mock(side_effect=lambda req: httpx.Response(
            200, json=USERS[req.headers["authorization"][7:]]) if req.headers.get("authorization", "")[7:] in USERS
            else httpx.Response(401, json={}))
        yield router


async def send(c, text="hi"):
    return await c.post("/bff/chat/messages", json={"text": text}, headers=H)


# ---------- session & configuration ----------

async def test_chat_requires_session_and_csrf(gw, gw_rest):
    app = make_app(gw, ScriptedProvider())
    async with app.router.lifespan_context(app):
        anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dash.test")
        assert (await anon.get("/bff/chat")).status_code == 401
        assert (await anon.post("/bff/chat/messages", json={"text": "x"}, headers=H)).status_code == 401
        c = await client_for(app, KEY_A)
        assert (await c.post("/bff/chat/messages", json={"text": "x"})).status_code == 403  # no CSRF header


async def test_no_provider_is_a_clear_error_not_a_fake_reply(gw, gw_rest):
    app = make_app(gw, None)
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        assert (await c.get("/bff/chat/status")).json()["available"] is False
        r = await send(c)
        assert r.status_code == 503 and r.json()["error"] == "provider_not_configured"
        assert gw.calls == [] and (await c.get("/bff/chat")).json()["messages"] == []


async def test_key_never_returned_and_gateway_gets_session_key(gw, gw_rest):
    p = ScriptedProvider(call("woocommerce__list_products", {"per_page": 5}), ProviderReply("Here are the products."))
    app = make_app(gw, p)
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        r = await send(c, "list products")
        assert r.status_code == 200 and KEY_A not in r.text
        assert gw.calls == [(KEY_A, "woocommerce__list_products", {"per_page": 5})]
        for path in ("/bff/chat", "/bff/chat/status"):
            assert KEY_A not in (await c.get(path)).text
        assert all(KEY_A not in json.dumps(m) for msgs, _ in p.seen for m in msgs)


# ---------- discovery, loops, validation ----------

async def test_model_gets_discovered_tools_and_schemas_per_user(gw, gw_rest):
    pa, pb = ScriptedProvider(ProviderReply("a")), ScriptedProvider(ProviderReply("b"))
    for key, p, expected in ((KEY_A, pa, {"woocommerce__list_products", "notes__list_notes", "notes__create_note", "notes__purge"}),
                             (KEY_B, pb, {"notes__list_notes"})):
        app = make_app(gw, p)
        async with app.router.lifespan_context(app):
            c = await client_for(app, key)
            await send(c)
        tools = p.seen[0][1]
        assert {t["function"]["name"] for t in tools} == expected
        if key == KEY_A:
            prod = next(t for t in tools if t["function"]["name"] == "woocommerce__list_products")
            assert prod["function"]["parameters"]["properties"]["per_page"]["maximum"] == 50


async def test_multi_step_tool_loop(gw, gw_rest):
    p = ScriptedProvider(call("woocommerce__list_products", {"per_page": 2}, "c1"),
                         call("notes__list_notes", {}, "c2"),
                         ProviderReply("Summary of products and notes."))
    gw.results["woocommerce__list_products"] = ToolOutcome(False, "2 products", {"products": [{"id": 11}]})
    app = make_app(gw, p)
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        r = (await send(c, "what do we sell and what notes exist?")).json()
    assert [n for _, n, _ in gw.calls] == ["woocommerce__list_products", "notes__list_notes"]
    second_prompt = p.seen[1][0]
    tool_msg = next(m for m in second_prompt if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "c1" and '"id":11' in tool_msg["content"] and "untrusted" in tool_msg["content"]
    assert r["messages"][-1] == {**r["messages"][-1], "role": "assistant", "text": "Summary of products and notes."}
    assert [a["status"] for a in r["activity"]] == ["success", "success"]


@pytest.mark.parametrize("reply,summary", [
    (call("woocommerce__delete_everything", {}), "Unknown tool"),
    (call("woocommerce__list_products", "{not json"), "Malformed"),
    (call("woocommerce__list_products", {"per_page": 999}), "Invalid arguments"),
    (call("woocommerce__list_products", {"evil": 1}), "Invalid arguments"),
    (call("woocommerce__list_products", "[1,2]"), "Invalid arguments"),
])
async def test_unknown_tools_and_bad_arguments_are_not_executed(gw, gw_rest, reply, summary):
    p = ScriptedProvider(reply, ProviderReply("Sorry."))
    app = make_app(gw, p)
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        r = (await send(c)).json()
    assert gw.calls == []
    assert r["activity"][0]["status"] == "failure" and summary in r["activity"][0]["summary"]
    tool_msg = next(m for m in p.seen[1][0] if m["role"] == "tool")
    assert tool_msg["content"].startswith("ERROR:")


async def test_member_cannot_reach_tools_the_gateway_did_not_offer(gw, gw_rest):
    p = ScriptedProvider(call("notes__create_note", {"title": "t", "body": "b"}), ProviderReply("no"))
    app = make_app(gw, p)
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_B)  # bob only has notes__list_notes
        r = (await send(c)).json()
    assert gw.calls == [] and r["pending"] is None and "Unknown tool" in r["activity"][0]["summary"]


async def test_tool_call_and_step_limits(gw, gw_rest):
    many = ProviderReply(None, [ToolCall(f"c{i}", "notes__list_notes", "{}") for i in range(5)])
    p = ScriptedProvider(many, *[call("notes__list_notes", {}, f"s{i}") for i in range(10)])
    app = make_app(gw, p, limits=ChatLimits(max_tool_calls=3, max_steps=2))
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        r = (await send(c)).json()
    assert len(gw.calls) == 3
    assert sum("limit" in a["summary"] for a in r["activity"]) >= 2
    assert r["messages"][-1]["role"] == "notice" and "Stopped after 2" in r["messages"][-1]["text"]


# ---------- failures ----------

@pytest.mark.parametrize("exc,status,code", [
    (ProviderError("timeout", "The language model did not respond within 60s."), 504, "provider_timeout"),
    (ProviderError("rate_limited", "rate limited"), 429, "provider_rate_limited"),
    (ProviderError("auth", "The language model provider rejected the server's credentials."), 502, "provider_auth"),
    (ProviderError("malformed", "bad"), 502, "provider_malformed"),
])
async def test_provider_errors_roll_back_the_turn(gw, gw_rest, exc, status, code):
    app = make_app(gw, ScriptedProvider(exc))
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        r = await send(c, "hello")
        assert r.status_code == status and r.json()["error"] == code and KEY_A not in r.text
        assert (await c.get("/bff/chat")).json()["messages"] == []  # user can simply retry


async def test_context_length_retries_with_shorter_window(gw, gw_rest):
    p = ScriptedProvider(ProviderError("context_length", "too long"), ProviderReply("ok after trimming"))
    app = make_app(gw, p)
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        r = await send(c)
    assert r.status_code == 200 and r.json()["messages"][-1]["text"] == "ok after trimming"


async def test_upstream_failures_and_denials_do_not_crash_chat(gw, gw_rest):
    gw.results["woocommerce__list_products"] = ToolOutcome(True, "Tool 'woocommerce__list_products' requires the admin role.", None)
    gw.results["notes__list_notes"] = GatewayUnavailable("the gateway did not respond within 45s")
    p = ScriptedProvider(call("woocommerce__list_products", {}, "c1"), call("notes__list_notes", {}, "c2"),
                         ProviderReply("Both tools failed."))
    app = make_app(gw, p)
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        r = (await send(c)).json()
    assert [a["status"] for a in r["activity"]] == ["failure", "failure"]
    assert "admin role" in r["activity"][1]["summary"] and "did not respond" in r["activity"][0]["summary"]
    assert r["messages"][-1]["text"] == "Both tools failed."


async def test_gateway_down_at_discovery(gw, gw_rest):
    gw.list_error = GatewayUnavailable("the gateway MCP endpoint is unavailable")
    app = make_app(gw, ScriptedProvider())
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        r = await send(c)
        assert r.status_code == 503 and r.json()["error"] == "gateway_unavailable"
        assert (await c.get("/bff/chat")).json()["messages"] == []


async def test_revoked_key_during_chat_ends_session_and_clears_history(gw, gw_rest):
    app = make_app(gw, ScriptedProvider(ProviderReply("first")))
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        assert (await send(c)).status_code == 200
        gw.list_error = GatewayAuthError("rejected")
        gw.tools[KEY_A] = []
        app.state.chat.conversation  # noqa: B018
        conv = next(iter(app.state.chat._conv.values()))
        conv.tools_at = 0  # force rediscovery
        r = await send(c, "again")
        assert r.status_code == 401 and r.json()["error"] == "key_rejected"
        assert (await c.get("/bff/session")).status_code == 401
        assert app.state.chat._conv == {}


# ---------- confirmations ----------

async def mutation_app(gw, *extra):
    p = ScriptedProvider(call("notes__create_note", {"title": "Hi", "body": "Body", "api_key": "x"} if False else
                              {"title": "Hi", "body": "Body"}, "m1"), *extra)
    return make_app(gw, p), p


async def test_mutation_returns_preview_without_executing(gw, gw_rest):
    app, _ = await mutation_app(gw)
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        r = (await send(c, "create a note")).json()
    assert gw.calls == []
    assert r["pending"]["tool"] == "notes__create_note" and r["pending"]["args"] == {"title": "Hi", "body": "Body"}
    assert r["activity"][0]["status"] == "awaiting_confirmation"


@pytest.mark.parametrize("tool", [SNEAKY, PRODUCTS.__class__("woocommerce__update_order_status", None, "x", {"type": "object"}, None, None)])
async def test_confirmation_is_the_default(gw, gw_rest, tool):
    gw.tools[KEY_A].append(tool) if tool not in gw.tools[KEY_A] else None
    app = make_app(gw, ScriptedProvider(call(tool.name, {})))
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        r = (await send(c)).json()
    assert r["pending"] is not None and gw.calls == []


async def test_confirm_executes_once_and_continues(gw, gw_rest):
    app, p = await mutation_app(gw, ProviderReply("Created your note."))
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        pend = (await send(c)).json()["pending"]
        r = await c.post(f"/bff/chat/actions/{pend['id']}/confirm", json={"digest": pend["digest"]}, headers=H)
        assert r.status_code == 200 and r.json()["messages"][-1]["text"] == "Created your note."
        again = await c.post(f"/bff/chat/actions/{pend['id']}/confirm", json={"digest": pend["digest"]}, headers=H)
        assert again.status_code in (404, 409)
    assert gw.calls == [(KEY_A, "notes__create_note", {"title": "Hi", "body": "Body"})]
    assert any(m["role"] == "tool" and m["tool_call_id"] == "m1" for m in p.seen[-1][0])


async def test_concurrent_double_confirm_executes_once(gw, gw_rest):
    gw.delay = 0.2
    app, _ = await mutation_app(gw, ProviderReply("done"))
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        pend = (await send(c)).json()["pending"]
        url = f"/bff/chat/actions/{pend['id']}/confirm"
        r1, r2 = await asyncio.gather(c.post(url, json={"digest": pend["digest"]}, headers=H),
                                      c.post(url, json={"digest": pend["digest"]}, headers=H))
    assert sorted([r1.status_code, r2.status_code]) == [200, 409]
    assert len(gw.calls) == 1


async def test_cancel_never_executes(gw, gw_rest):
    app, p = await mutation_app(gw, ProviderReply("Okay, I did not create it."))
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        pend = (await send(c)).json()["pending"]
        r = await c.post(f"/bff/chat/actions/{pend['id']}/cancel", headers=H)
        assert r.status_code == 200 and r.json()["pending"] is None
        assert (await c.post(f"/bff/chat/actions/{pend['id']}/confirm", json={"digest": pend["digest"]}, headers=H)).status_code == 404
    assert gw.calls == []
    assert any("cancelled" in m.get("content", "") for m in p.seen[-1][0] if m["role"] == "tool")


async def test_expired_action_never_executes(gw, gw_rest):
    app, _ = await mutation_app(gw, ProviderReply("next"))
    app.state.chat.limits = ChatLimits(pending_ttl=0.1)
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        pend = (await send(c)).json()["pending"]
        assert pend is not None
        await asyncio.sleep(0.2)
        r = await c.post(f"/bff/chat/actions/{pend['id']}/confirm", json={"digest": pend["digest"]}, headers=H)
        assert r.status_code == 410
        assert (await c.get("/bff/chat")).json()["pending"] is None
    assert gw.calls == []


async def test_new_message_after_expiry_keeps_history_well_formed(gw, gw_rest):
    app, p = await mutation_app(gw, ProviderReply("Fresh answer."))
    app.state.chat.limits = ChatLimits(pending_ttl=0)
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        await send(c)
        await asyncio.sleep(0.01)
        r = await send(c, "never mind")
        assert r.status_code == 200
    msgs = p.seen[-1][0]
    call_ids = {tc["id"] for m in msgs if m["role"] == "assistant" for tc in m.get("tool_calls", [])}
    answered = {m["tool_call_id"] for m in msgs if m["role"] == "tool"}
    assert call_ids <= answered and gw.calls == []


async def test_pending_action_is_bound_to_session_and_digest(gw, gw_rest):
    app, _ = await mutation_app(gw)
    async with app.router.lifespan_context(app):
        a = await client_for(app, KEY_A)
        pend = (await send(a)).json()["pending"]
        b = await client_for(app, KEY_B)
        a2 = await client_for(app, KEY_A)  # same user, different session
        for other in (b, a2):
            r = await other.post(f"/bff/chat/actions/{pend['id']}/confirm", json={"digest": pend["digest"]}, headers=H)
            assert r.status_code == 404
        r = await a.post(f"/bff/chat/actions/{pend['id']}/confirm", json={"digest": "0" * 64}, headers=H)
        assert r.status_code == 409 and r.json()["error"] == "action_changed"
        assert (await a.post("/bff/chat/actions/../confirm", json={}, headers=H)).status_code in (404, 405)
    assert gw.calls == []


async def test_confirm_revalidates_tool_availability(gw, gw_rest):
    app, _ = await mutation_app(gw, ProviderReply("ok"))
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        pend = (await send(c)).json()["pending"]
        gw.tools[KEY_A] = [t for t in gw.tools[KEY_A] if t.name != "notes__create_note"]  # e.g. upstream disabled
        r = (await c.post(f"/bff/chat/actions/{pend['id']}/confirm", json={"digest": pend["digest"]}, headers=H)).json()
    assert gw.calls == [] and "no longer available" in r["activity"][0]["summary"].lower()


async def test_message_while_action_pending_is_refused(gw, gw_rest):
    app, _ = await mutation_app(gw)
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        await send(c)
        r = await send(c, "something else")
        assert r.status_code == 409 and r.json()["error"] == "pending_action"


# ---------- isolation, reset, logout, sanitisation ----------

async def test_conversations_are_isolated_and_cleared_on_logout(gw, gw_rest):
    app = make_app(gw, ScriptedProvider(ProviderReply("secret answer for alice"), ProviderReply("bob answer")))
    async with app.router.lifespan_context(app):
        a = await client_for(app, KEY_A)
        b = await client_for(app, KEY_B)
        await send(a, "alice question")
        assert "alice" not in (await b.get("/bff/chat")).text
        await send(b, "bob question")
        assert "bob" not in (await a.get("/bff/chat")).text.replace("alice", "")
        await a.post("/bff/logout", headers=H)
        assert len(app.state.chat._conv) == 1
        r = await a.post("/bff/chat/messages", json={"text": "x", "user_id": "ub"}, headers=H)
        assert r.status_code == 401


async def test_reset(gw, gw_rest):
    app = make_app(gw, ScriptedProvider(ProviderReply("hello")))
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        await send(c)
        r = await c.post("/bff/chat/reset", headers=H)
        assert r.json()["messages"] == [] and r.json()["activity"] == []


def test_sanitize_args():
    out = sanitize_args({"api_key": "s3cret", "token": "t", "Password": "p", "title": "x" * 500, "nested": {"secret": 1, "ok": [1, 2]}})
    assert out["api_key"] == out["token"] == out["Password"] == "***" and out["nested"]["secret"] == "***"
    assert len(out["title"]) <= 201 and out["nested"]["ok"] == [1, 2]


async def test_activity_is_sanitized_and_concise(gw, gw_rest):
    tool = ToolSpec("notes__search", None, None, {"type": "object"}, True, None)
    gw.tools[KEY_A].append(tool)
    gw.results["notes__search"] = ToolOutcome(False, "<script>alert(1)</script>" + "y" * 5000, None)
    p = ScriptedProvider(call("notes__search", {"query": "q", "token": "should-not-show"}), ProviderReply("ok"))
    app = make_app(gw, p)
    async with app.router.lifespan_context(app):
        c = await client_for(app, KEY_A)
        r = await send(c)
    a = r.json()["activity"][0]
    assert a["args"] == {"query": "q", "token": "***"} and "should-not-show" not in r.text
    assert len(a["summary"]) < 400  # concise; rendered as text by the UI
    assert len(next(m for m in p.seen[1][0] if m["role"] == "tool")["content"]) < 6200
