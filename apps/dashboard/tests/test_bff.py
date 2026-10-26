import json
import logging

import httpx
import pytest
import respx

from dashboard_bff.app import COOKIE, create_app

GW = "http://gateway.test:8080"
KEY = "urumi_0123456789ab_" + "A" * 43
H = {"X-Urumi-Dashboard": "1"}
ME = {"id": "u1", "name": "alice", "role": "member", "disabled": False, "created_at": "2026-10-24T00:00:00Z"}


@pytest.fixture
def gw():
    with respx.mock(base_url=GW, assert_all_called=False) as router:
        router.get("/api/v1/me").mock(side_effect=lambda req: httpx.Response(200, json=ME)
                                      if req.headers.get("authorization") == f"Bearer {KEY}" else httpx.Response(401, json={}))
        yield router


@pytest.fixture
def app(tmp_path):
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><div id=root></div>")
    return create_app(gateway_url=GW, static_dir=str(web), secure_cookies=True, idle_ttl=60, absolute_ttl=3600)


@pytest.fixture
async def client(app):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://dash.test") as c:
            yield c


async def login(client):
    r = await client.post("/bff/login", json={"api_key": KEY}, headers=H)
    assert r.status_code == 200, r.text
    return r


async def test_login_sets_strict_httponly_cookie_and_hides_key(client, gw):
    r = await login(client)
    cookie = r.headers["set-cookie"]
    assert f"{COOKIE}=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" in cookie
    assert KEY not in r.text and KEY not in cookie
    s = await client.get("/bff/session")
    assert s.json() == {"user": ME} and KEY not in s.text


@pytest.mark.parametrize("body,status,code", [
    ({"api_key": "nope"}, 400, "invalid_key_format"),
    ({"api_key": "urumi_0123456789ab_" + "B" * 43}, 401, "key_rejected"),
    ({}, 400, "invalid_key_format"),
])
async def test_login_failures(client, gw, body, status, code):
    r = await client.post("/bff/login", json=body, headers=H)
    assert r.status_code == status and r.json()["error"] == code
    assert "set-cookie" not in r.headers


async def test_login_requires_csrf_header_and_same_origin(client, gw):
    assert (await client.post("/bff/login", json={"api_key": KEY})).status_code == 403
    r = await client.post("/bff/login", json={"api_key": KEY}, headers={**H, "Origin": "https://evil.example"})
    assert r.status_code == 403
    r = await client.post("/bff/login", json={"api_key": KEY}, headers={**H, "Origin": "https://dash.test"})
    assert r.status_code == 200


async def test_login_rate_limited(client, gw):
    for _ in range(10):
        await client.post("/bff/login", json={"api_key": "urumi_0123456789ab_" + "B" * 43}, headers=H)
    r = await client.post("/bff/login", json={"api_key": KEY}, headers=H)
    assert r.status_code == 429


async def test_login_gateway_down(app, client):
    with respx.mock(base_url=GW) as router:
        router.get("/api/v1/me").mock(side_effect=httpx.ConnectError("down"))
        r = await client.post("/bff/login", json={"api_key": KEY}, headers=H)
    assert r.status_code == 503 and r.json()["error"] == "gateway_unavailable"


async def test_proxy_adds_session_key_and_never_returns_it(client, gw):
    route = gw.get("/api/v1/tools").mock(return_value=httpx.Response(200, json=[{"name": "notes__x"}]))
    await login(client)
    r = await client.get("/api/v1/tools", headers={"Authorization": "Bearer something-from-browser"})
    assert r.status_code == 200 and r.json() == [{"name": "notes__x"}]
    assert route.calls.last.request.headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in r.text


async def test_proxy_requires_session(client, gw):
    r = await client.get("/api/v1/tools")
    assert r.status_code == 401 and r.json()["error"] == "no_session"


@pytest.mark.parametrize("method,path,status", [
    ("GET", "/api/v1/users/%2e%2e/admin", 404), ("GET", "/api/v1/openapi.json", 404),
    ("GET", "/api/v1/users/x/keys/y", 404), ("PUT", "/api/v1/users/x", 405),
    ("DELETE", "/api/v1/users/x", 405), ("POST", "/api/v1/me", 405),
])
async def test_proxy_allow_list(client, gw, method, path, status):
    await login(client)
    r = await client.request(method, path, headers=H)
    assert r.status_code == status


async def test_proxy_writes_need_csrf(client, gw):
    gw.post("/api/v1/users").mock(return_value=httpx.Response(201, json={"id": "u2"}))
    await login(client)
    assert (await client.post("/api/v1/users", json={"name": "bob"})).status_code == 403
    assert (await client.post("/api/v1/users", json={"name": "bob"}, headers=H)).status_code == 201


async def test_gateway_authorisation_is_passed_through(client, gw):
    gw.get("/api/v1/users").mock(return_value=httpx.Response(403, json={"detail": "This operation requires the admin role."}))
    await login(client)
    r = await client.get("/api/v1/users")
    assert r.status_code == 403 and "admin role" in r.text


async def test_revoked_key_ends_session(client, gw):
    gw.get("/api/v1/tools").mock(return_value=httpx.Response(401, json={"detail": "x"}))
    await login(client)
    r = await client.get("/api/v1/tools")
    assert r.status_code == 401 and r.json()["error"] == "key_rejected"
    assert (await client.get("/bff/session")).status_code == 401


async def test_idle_session_expires(tmp_path, gw):
    app = create_app(gateway_url=GW, static_dir=str(tmp_path), secure_cookies=False, idle_ttl=0, absolute_ttl=3600)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://dash.test") as c:
            await login(c)
            r = await c.get("/bff/session")
            assert r.status_code == 401


async def test_logout(client, gw):
    await login(client)
    assert (await client.post("/bff/logout", headers=H)).status_code == 200
    assert (await client.get("/bff/session")).status_code == 401
    assert len(client._transport.app.state.sessions) == 0  # type: ignore[attr-defined]


async def test_audit_params_filtered(client, gw):
    route = gw.get("/api/v1/audit").mock(return_value=httpx.Response(200, json=[]))
    await login(client)
    await client.get("/api/v1/audit", params={"limit": 50, "success": "false", "evil": "1"})
    assert dict(route.calls.last.request.url.params) == {"limit": "50", "success": "false"}


async def test_new_key_response_passes_through_once_and_is_not_logged(client, gw, caplog):
    caplog.set_level(logging.DEBUG)
    raw = "urumi_aaaaaaaaaaaa_" + "Z" * 43
    gw.post("/api/v1/users/u2/keys").mock(return_value=httpx.Response(201, json={"id": "aaaaaaaaaaaa", "key": raw}))
    await login(client)
    r = await client.post("/api/v1/users/u2/keys", json={"name": "x"}, headers=H)
    assert r.json()["key"] == raw
    assert raw not in caplog.text and KEY not in caplog.text


async def test_health_reports_gateway(client):
    with respx.mock(base_url=GW) as router:
        router.get("/readyz").mock(return_value=httpx.Response(200, json={"status": "ready"}))
        assert (await client.get("/bff/health")).json() == {"dashboard": "ok", "gateway": {"ready": True, "status": "ready"}}
        router.get("/readyz").mock(side_effect=httpx.ConnectError("x"))
        assert (await client.get("/bff/health")).json()["gateway"]["ready"] is False


async def test_security_headers_and_spa_fallback(client, gw):
    r = await client.get("/team")
    assert r.status_code == 200 and "root" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
    assert (await client.get("/bff/unknown")).status_code in (404, 405)


async def test_gateway_url_required():
    with pytest.raises(SystemExit):
        create_app(gateway_url="")


def test_session_repr_hides_key():
    from dashboard_bff.app import Session
    assert KEY not in repr(Session(key=KEY, user=ME, created=0, last_seen=0))
    assert json  # keep import used
