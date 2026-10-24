import pytest

from gateway.ssrf import UnsafeURL, validate_url
from gateway.upstreams import UpstreamError

from .conftest import add_upstream, tool

# ---------- admin management ----------


async def test_admin_crud_and_no_credential_values(http, admin, connector):
    connector.tools["notes"] = [tool("create_note")]
    up = await add_upstream(http, admin, "notes", credential_ref="notes-token")
    assert up["credential_ref"] == "notes-token" and up["tool_count"] == 1 and up["reachable"] is True
    body = str((await http.get("/api/v1/upstreams", headers=admin["h"])).json())
    assert "notes-secret-token-value" not in body

    r = await http.patch("/api/v1/upstreams/notes", json={"display_name": "Team notes"}, headers=admin["h"])
    assert r.json()["display_name"] == "Team notes"
    r = await http.patch("/api/v1/upstreams/notes", json={"clear_credential": True}, headers=admin["h"])
    assert r.json()["credential_ref"] is None
    assert (await http.delete("/api/v1/upstreams/notes", headers=admin["h"])).status_code == 204
    assert (await http.get("/api/v1/upstreams/notes", headers=admin["h"])).status_code == 404
    assert (await http.get("/api/v1/tools", headers=admin["h"])).json() == []


@pytest.mark.parametrize("slug", ["Notes", "no_underscore", "a__b", "-x", "x-", "1abc", "", "a" * 40, "a--b"])
async def test_invalid_slugs_rejected(http, admin, slug):
    r = await http.post("/api/v1/upstreams", json={"slug": slug, "display_name": "x",
                                                   "url": "http://x.ns.svc.cluster.local:8000/mcp"}, headers=admin["h"])
    assert r.status_code == 422


async def test_duplicate_slug_rejected(http, admin):
    await add_upstream(http, admin, "notes")
    r = await http.post("/api/v1/upstreams", json={"slug": "notes", "display_name": "Other",
                                                   "url": "http://other.ns.svc.cluster.local:8000/mcp"}, headers=admin["h"])
    assert r.status_code == 409


@pytest.mark.parametrize("ref", ["../etc/passwd", "UPPER", "a/b", ".hidden", "x" * 70])
async def test_bad_credential_ref_rejected(http, admin, ref):
    r = await http.post("/api/v1/upstreams", json={"slug": "x", "display_name": "x", "credential_ref": ref,
                                                   "url": "http://x.ns.svc.cluster.local:8000/mcp"}, headers=admin["h"])
    assert r.status_code == 422


# ---------- SSRF ----------

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://localhost:8000/mcp", "http://127.0.0.1:8000/mcp", "http://[::1]:8000/mcp",
    "http://0.0.0.0:8000/mcp", "http://10.42.0.5.nip.io:8000/mcp",
    "https://example.com/mcp", "http://evil.svc.cluster.local.attacker.com:8000/mcp",
    "http://user:pass@notes.urumi.svc.cluster.local:8000/mcp",
    "http://notes.urumi.svc.cluster.local:22/mcp", "http://notes.urumi.svc.cluster.local:8000/mcp?x=1",
    "file:///etc/passwd", "gopher://notes.urumi.svc.cluster.local:8000/", "ftp://x.svc.cluster.local/",
    "http://192.168.1.10:8000/mcp", "http://[::ffff:169.254.169.254]/", "http://notes.urumi.svc.cluster.local:8000/m cp",
])
async def test_ssrf_rejected_via_api(http, admin, url):
    r = await http.post("/api/v1/upstreams", json={"slug": "x", "display_name": "x", "url": url}, headers=admin["h"])
    assert r.status_code == 422, url


async def test_dns_resolving_to_forbidden_address_rejected(settings, fake_dns):
    fake_dns["rebind.urumi.svc.cluster.local"] = "169.254.169.254"
    with pytest.raises(UnsafeURL):
        await validate_url("http://rebind.urumi.svc.cluster.local:8000/mcp", settings)
    fake_dns["loop.urumi.svc.cluster.local"] = "127.0.0.1"
    with pytest.raises(UnsafeURL):
        await validate_url("http://loop.urumi.svc.cluster.local:8000/mcp", settings)
    fake_dns["outside.urumi.svc.cluster.local"] = "8.8.8.8"
    with pytest.raises(UnsafeURL):
        await validate_url("http://outside.urumi.svc.cluster.local:8000/mcp", settings)


async def test_allowed_in_cluster_urls(settings, fake_dns):
    await validate_url("http://notes.urumi-gateway.svc.cluster.local:8000/mcp", settings)
    await validate_url("http://10.43.12.7:8000/mcp", settings)


async def test_patch_url_is_validated(http, admin):
    await add_upstream(http, admin, "notes")
    r = await http.patch("/api/v1/upstreams/notes", json={"url": "http://169.254.169.254/"}, headers=admin["h"])
    assert r.status_code == 422


# ---------- discovery, namespacing, collisions ----------

async def test_namespacing_across_multiple_upstreams(http, admin, connector):
    connector.tools = {"notes": [tool("search"), tool("create")], "shop": [tool("search")], "gh": [tool("search")]}
    for slug in ("notes", "shop", "gh"):
        await add_upstream(http, admin, slug)
    names = [t["name"] for t in (await http.get("/api/v1/tools", headers=admin["h"])).json()]
    assert names == ["gh__search", "notes__create", "notes__search", "shop__search"]


async def test_duplicate_and_invalid_tool_names_are_not_registered(http, admin, connector):
    connector.tools["notes"] = [tool("dup"), tool("dup"), tool("ok"), tool("bad name"), tool("x" * 130)]
    up = await add_upstream(http, admin, "notes")
    names = [t["name"] for t in (await http.get("/api/v1/tools", headers=admin["h"])).json()]
    assert names == ["notes__ok"]
    assert "duplicate tool name 'dup'" in up["last_error"] and "invalid tool name" in up["last_error"]


async def test_rediscovery_replaces_upstream_tools(http, admin, connector):
    connector.tools["notes"] = [tool("a"), tool("b")]
    await add_upstream(http, admin, "notes")
    connector.tools["notes"] = [tool("b"), tool("c")]
    await http.post("/api/v1/upstreams/notes/discover", headers=admin["h"])
    names = [t["name"] for t in (await http.get("/api/v1/tools", headers=admin["h"])).json()]
    assert names == ["notes__b", "notes__c"]


async def test_unavailable_and_slow_upstreams_do_not_break_healthy_ones(app, http, admin, connector):
    connector.tools = {"good": [tool("ping")], "down": [tool("x")], "slow": [tool("y")]}
    for slug in ("good", "down", "slow"):
        await add_upstream(http, admin, slug)
    connector.list_behaviour = {"down": UpstreamError("connection", "ConnectError: refused"), "slow": "hang"}
    results = {r.slug: r for r in await app.state.manager.discover_all()}
    assert results["good"].ok and not results["down"].ok and not results["slow"].ok
    assert "timeout" in results["slow"].error

    ups = {u["slug"]: u for u in (await http.get("/api/v1/upstreams", headers=admin["h"])).json()}
    assert ups["good"]["reachable"] is True
    assert ups["down"]["reachable"] is False and "ConnectError" in ups["down"]["last_error"]
    assert ups["slow"]["reachable"] is False and "timeout" in ups["slow"]["last_error"]


async def test_new_upstream_that_never_answered_exposes_no_tools(app, http, admin, connector):
    connector.list_behaviour["down"] = UpstreamError("connection", "refused")
    connector.tools["down"] = [tool("x")]
    up = await add_upstream(http, admin, "down")
    assert up["reachable"] is False and up["tool_count"] == 0
    assert (await http.get("/api/v1/tools", headers=admin["h"])).json() == []


async def test_error_details_are_sanitized(app, http, admin, connector):
    connector.list_behaviour["bad"] = UpstreamError("protocol", "401 Authorization: Bearer ghp_" + "a" * 36)
    await add_upstream(http, admin, "bad")
    up = (await http.get("/api/v1/upstreams/bad", headers=admin["h"])).json()
    assert "ghp_" + "a" * 36 not in up["last_error"] and "***" in up["last_error"]


async def test_disable_removes_tools_immediately(http, admin, member, connector):
    connector.tools["notes"] = [tool("list")]
    await add_upstream(http, admin, "notes")
    assert len((await http.get("/api/v1/tools", headers=member["h"])).json()) == 1
    await http.patch("/api/v1/upstreams/notes", json={"enabled": False}, headers=admin["h"])
    assert (await http.get("/api/v1/tools", headers=member["h"])).json() == []
    # Re-enabling discovers again.
    await http.patch("/api/v1/upstreams/notes", json={"enabled": True}, headers=admin["h"])
    assert len((await http.get("/api/v1/tools", headers=member["h"])).json()) == 1
