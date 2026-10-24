import sqlite3

import pytest

from gateway.security import generate_key, hash_key, parse_key, sanitize

from .conftest import PEPPER

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
MCP_H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def test_key_format_and_entropy():
    keys = {generate_key(PEPPER.encode()).raw for _ in range(200)}
    assert len(keys) == 200
    k = generate_key(PEPPER.encode())
    assert parse_key(k.raw) == k.key_id and len(k.raw) == len("urumi_") + 12 + 1 + 43
    assert k.key_hash == hash_key(PEPPER.encode(), k.raw) and k.raw not in k.key_hash
    assert hash_key(b"other-pepper" * 4, k.raw) != k.key_hash


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic Zm9vOmJhcg=="}, {"Authorization": "Bearer"},
                                     {"Authorization": "Bearer not-a-key"},
                                     {"Authorization": "Bearer urumi_000000000000_" + "A" * 43}])
async def test_missing_or_invalid_key_rejected(http, admin, headers):
    assert (await http.get("/api/v1/me", headers=headers)).status_code == 401
    r = await http.post("/mcp", json=INIT, headers={**MCP_H, **headers})
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")


async def test_wrong_secret_for_existing_key_id(http, admin):
    key_id = parse_key(admin["key"])
    forged = f"urumi_{key_id}_" + "B" * 43
    assert (await http.get("/api/v1/me", headers={"Authorization": f"Bearer {forged}"})).status_code == 401


async def test_revoked_key_rejected_on_next_request(http, admin, member):
    assert (await http.get("/api/v1/me", headers=member["h"])).status_code == 200
    key_id = parse_key(member["key"])
    assert (await http.delete(f"/api/v1/keys/{key_id}", headers=admin["h"])).status_code == 204
    assert (await http.get("/api/v1/me", headers=member["h"])).status_code == 401
    assert (await http.post("/mcp", json=INIT, headers={**MCP_H, **member["h"]})).status_code == 401


async def test_disabled_user_rejected(http, admin, member):
    r = await http.patch(f"/api/v1/users/{member['id']}", json={"disabled": True}, headers=admin["h"])
    assert r.status_code == 200
    assert (await http.get("/api/v1/me", headers=member["h"])).status_code == 401


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/v1/users", None), ("post", "/api/v1/users", {"name": "bob"}),
    ("get", "/api/v1/users/X", None), ("patch", "/api/v1/users/X", {"role": "admin"}),
    ("post", "/api/v1/users/X/keys", {}), ("get", "/api/v1/users/X/keys", None), ("delete", "/api/v1/keys/abc", None),
    ("get", "/api/v1/upstreams", None), ("post", "/api/v1/upstreams", {"slug": "x", "display_name": "x", "url": "http://x"}),
    ("patch", "/api/v1/upstreams/x", {"enabled": False}), ("delete", "/api/v1/upstreams/x", None),
    ("post", "/api/v1/upstreams/x/discover", None), ("get", "/api/v1/audit", None), ("get", "/api/v1/admin/status", None),
])
async def test_member_cannot_use_admin_endpoints(http, member, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    r = await getattr(http, method)(path, headers=member["h"], **kwargs)
    assert r.status_code == 403


async def test_member_sees_only_self(http, admin, member):
    me = (await http.get("/api/v1/me", headers=member["h"])).json()
    assert me["id"] == member["id"] and me["role"] == "member"
    keys = (await http.get("/api/v1/me/keys", headers=member["h"])).json()
    assert len(keys) == 1 and "key" not in keys[0] and "key_hash" not in keys[0]
    # Admin's key id is not discoverable by the member.
    assert parse_key(admin["key"]) not in str(keys)


async def test_new_key_shown_once_and_only_hash_stored(http, admin, member, settings):
    r = await http.post(f"/api/v1/users/{member['id']}/keys", json={"name": "laptop"}, headers=admin["h"])
    assert r.status_code == 201
    raw = r.json()["key"]
    listed = (await http.get(f"/api/v1/users/{member['id']}/keys", headers=admin["h"])).json()
    assert raw not in str(listed) and all("key" not in k for k in listed)
    db = settings.database_url.split("///", 1)[1]
    blob = open(db, "rb").read()
    con = sqlite3.connect(db)
    hashes = [h for (h,) in con.execute("select key_hash from api_keys")]
    con.close()
    for k in (raw, admin["key"], member["key"]):
        assert k.encode() not in blob and k.split("_", 2)[2].encode() not in blob
    assert hash_key(PEPPER.encode(), raw) in hashes
    # The new key works.
    assert (await http.get("/api/v1/me", headers={"Authorization": f"Bearer {raw}"})).status_code == 200


async def test_last_admin_cannot_be_demoted(http, admin):
    r = await http.patch(f"/api/v1/users/{admin['id']}", json={"role": "member"}, headers=admin["h"])
    assert r.status_code == 409


async def test_role_change_takes_effect_immediately(http, admin, member):
    await http.patch(f"/api/v1/users/{member['id']}", json={"role": "admin"}, headers=admin["h"])
    assert (await http.get("/api/v1/users", headers=member["h"])).status_code == 200


def test_sanitize_masks_credentials():
    text = ("Authorization: Bearer abc.def ghp_" + "x" * 36 + " urumi_0123456789ab_" + "Z" * 43 +
            " https://user:pw@host/x?token=sekret ck_" + "a" * 40 + "\nline2")
    out = sanitize(text, 1000)
    for bad in ("abc.def", "ghp_x", "Z" * 43, "user:pw", "sekret", "a" * 40, "\n"):
        assert bad not in out


async def test_cli_bootstrap(monkeypatch, settings, capsys):
    from gateway import cli

    monkeypatch.setenv("GATEWAY_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("GATEWAY_KEY_PEPPER", PEPPER)
    monkeypatch.setattr("sys.argv", ["gateway.cli", "create-admin", "--name", "ops"])
    assert await cli._run(cli_args("create-admin", name="ops")) == 0
    out = capsys.readouterr().out
    assert "api_key: urumi_" in out
    # Second bootstrap is refused: no silent extra admins.
    assert await cli._run(cli_args("create-admin", name="ops2")) == 2


def cli_args(cmd, **kw):
    import argparse
    return argparse.Namespace(cmd=cmd, key_name="break-glass", **kw)


async def test_startup_waits_for_database(settings, connector, fake_dns, monkeypatch):
    """Schema init is retried (not a crash) while the database is unavailable."""
    import asyncio

    from gateway import app as app_module

    calls = {"n": 0}
    real = app_module.init_schema

    async def flaky(engine):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionRefusedError("db starting")
        await real(engine)

    monkeypatch.setattr(app_module, "init_schema", flaky)
    monkeypatch.setattr(app_module.anyio, "sleep", lambda s: asyncio.sleep(0))
    application = app_module.create_app(settings, connector=connector, background_discovery=False)
    async with application.router.lifespan_context(application):
        pass
    assert calls["n"] == 3
