"""Backend-for-frontend for the Urumi dashboard.

* The browser logs in with the user's own gateway API key once. The BFF checks
  it against the gateway (GET /api/v1/me) and keeps it only in server memory,
  bound to an opaque, HttpOnly, SameSite=Strict session cookie. The key is
  never returned to the browser, stored in it, or logged.
* /api/v1/* requests are forwarded only if they match the gateway's real
  routes (method + path allow-list), with the session's key attached. The
  gateway decides authorisation; the BFF only authenticates the session.
* A gateway 401 (invalid/revoked key, disabled user) ends the session.
* State-changing requests must carry `X-Urumi-Dashboard: 1` and, if the
  browser sends an Origin header, it must match the request host (CSRF).

Run with: uvicorn --factory dashboard_bff.app:create_app --host 0.0.0.0 --port 8090
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .chat import ChatError, ChatLimits, ChatService
from .gateway_mcp import GatewayAuthError, GatewayTools, McpGatewayClient
from .llm import ChatProvider, provider_from_env

log = logging.getLogger("dashboard_bff")
_ACTION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

COOKIE = "urumi_session"
CSRF_HEADER = "x-urumi-dashboard"
_KEY_RE = re.compile(r"^urumi_[0-9a-f]{12}_[A-Za-z0-9_-]{43}$")
_ID = r"[A-Za-z0-9_-]{1,64}"
_SLUG = r"[a-z][a-z0-9-]{0,31}"

# Exactly the gateway routes the dashboard uses (gateway/api.py). Anything else -> 404.
ROUTES: list[tuple[str, re.Pattern[str]]] = [
    (m, re.compile(f"^{p}$")) for m, p in [
        ("GET", r"/api/v1/me"), ("GET", r"/api/v1/me/keys"), ("GET", r"/api/v1/tools"),
        ("GET", r"/api/v1/users"), ("POST", r"/api/v1/users"),
        ("GET", rf"/api/v1/users/{_ID}"), ("PATCH", rf"/api/v1/users/{_ID}"),
        ("GET", rf"/api/v1/users/{_ID}/keys"), ("POST", rf"/api/v1/users/{_ID}/keys"),
        ("DELETE", rf"/api/v1/keys/{_ID}"),
        ("GET", r"/api/v1/upstreams"), ("POST", r"/api/v1/upstreams"),
        ("GET", rf"/api/v1/upstreams/{_SLUG}"), ("PATCH", rf"/api/v1/upstreams/{_SLUG}"),
        ("DELETE", rf"/api/v1/upstreams/{_SLUG}"), ("POST", rf"/api/v1/upstreams/{_SLUG}/discover"),
        ("GET", r"/api/v1/audit"), ("GET", r"/api/v1/admin/status"),
    ]
]
AUDIT_PARAMS = {"limit", "user_id", "upstream", "success", "event"}
MAX_BODY = 64 * 1024


@dataclass
class Session:
    key: str = field(repr=False)
    user: dict
    created: float
    last_seen: float


class SessionStore:
    """In-memory sessions (single replica; restart = everyone logs in again)."""

    def __init__(self, idle_ttl: float, absolute_ttl: float, max_sessions: int = 1000, on_drop=None):
        self.idle_ttl, self.absolute_ttl, self.max_sessions = idle_ttl, absolute_ttl, max_sessions
        self._s: dict[str, Session] = {}
        self._on_drop = on_drop or (lambda sid: None)  # e.g. clear that session's chat state

    def _remove(self, sid: str) -> None:
        if self._s.pop(sid, None) is not None:
            self._on_drop(sid)

    def create(self, key: str, user: dict) -> str:
        self._expire()
        if len(self._s) >= self.max_sessions:  # drop the oldest
            oldest = min(self._s, key=lambda k: self._s[k].last_seen)
            self._remove(oldest)
        sid = secrets.token_urlsafe(32)
        now = time.monotonic()
        self._s[sid] = Session(key=key, user=user, created=now, last_seen=now)
        return sid

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        s = self._s.get(sid)
        if s is None:
            return None
        now = time.monotonic()
        if now - s.last_seen > self.idle_ttl or now - s.created > self.absolute_ttl:
            self._remove(sid)
            return None
        s.last_seen = now
        return s

    def drop(self, sid: str | None) -> None:
        if sid:
            self._remove(sid)

    def _expire(self) -> None:
        now = time.monotonic()
        for sid in [k for k, s in self._s.items()
                    if now - s.last_seen > self.idle_ttl or now - s.created > self.absolute_ttl]:
            self._remove(sid)

    def __len__(self) -> int:
        return len(self._s)


class LoginLimiter:
    """At most `limit` login attempts per client per `window` seconds."""

    def __init__(self, limit: int = 10, window: float = 60.0):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque[float]] = {}

    def allow(self, client: str) -> bool:
        now = time.monotonic()
        q = self._hits.setdefault(client, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


async def _async(value):
    return value


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def create_app(*, gateway_url: str | None = None, static_dir: str | None = None,
               secure_cookies: bool | None = None, idle_ttl: float | None = None,
               absolute_ttl: float | None = None, transport: httpx.AsyncBaseTransport | None = None,
               chat_provider: ChatProvider | None | str = "env", gateway_tools: GatewayTools | None = None,
               chat_limits: ChatLimits | None = None, read_only_tools: frozenset[str] | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    gateway_url = (gateway_url or os.environ.get("GATEWAY_URL", "")).rstrip("/")
    if not gateway_url.startswith(("http://", "https://")):
        raise SystemExit("GATEWAY_URL must be set to the gateway's in-cluster URL")
    static = Path(static_dir or os.environ.get("DASHBOARD_STATIC_DIR", "/app/web"))
    secure = secure_cookies if secure_cookies is not None else os.environ.get("DASHBOARD_SECURE_COOKIES", "true").lower() == "true"
    idle = idle_ttl if idle_ttl is not None else float(os.environ.get("DASHBOARD_SESSION_IDLE_SECONDS", "1800"))
    absolute = absolute_ttl if absolute_ttl is not None else float(os.environ.get("DASHBOARD_SESSION_MAX_SECONDS", "28800"))

    provider = provider_from_env() if chat_provider == "env" else chat_provider
    ro = read_only_tools if read_only_tools is not None else frozenset(
        t.strip() for t in os.environ.get("CHAT_READONLY_TOOLS", "").split(",") if t.strip())
    limits = chat_limits or ChatLimits(max_steps=int(os.environ.get("CHAT_MAX_STEPS", "6")),
                                       max_tool_calls=int(os.environ.get("CHAT_MAX_TOOL_CALLS", "8")))
    chat = ChatService(provider, gateway_tools or McpGatewayClient(gateway_url, transport=transport),
                       read_only_tools=ro, limits=limits)
    sessions = SessionStore(idle, absolute, on_drop=chat.drop)
    limiter = LoginLimiter()
    state: dict[str, httpx.AsyncClient] = {}

    @asynccontextmanager
    async def lifespan(_):
        state["http"] = httpx.AsyncClient(
            base_url=gateway_url, timeout=httpx.Timeout(connect=3, read=40, write=10, pool=3),
            follow_redirects=False, trust_env=False, transport=transport,
            headers={"User-Agent": "urumi-dashboard-bff/0.1"},
        )
        try:
            yield
        finally:
            await state.pop("http").aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessions = sessions
    app.state.chat = chat
    log.info("chat provider: %s; operator read-only tools: %d",
             f"configured ({provider.model})" if provider else "not configured", len(ro))

    def set_cookie(resp: Response, sid: str) -> None:
        resp.set_cookie(COOKIE, sid, httponly=True, secure=secure, samesite="strict", path="/", max_age=int(absolute))

    def clear_cookie(resp: Response) -> None:
        resp.delete_cookie(COOKIE, path="/", httponly=True, secure=secure, samesite="strict")

    def csrf_ok(request: Request) -> bool:
        if request.headers.get(CSRF_HEADER) != "1":
            return False
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "")
            if urlsplit(origin).netloc != host:
                return False
        return True

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return resp

    # ---------- health ----------

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    @app.get("/bff/health")
    async def health():
        """Dashboard + gateway readiness, without credentials (gateway /readyz is unauthenticated)."""
        try:
            r = await state["http"].get("/readyz", timeout=3)
            gateway = {"ready": r.status_code == 200, "status": (r.json() or {}).get("status", "unknown")}
        except (httpx.HTTPError, ValueError) as exc:
            gateway = {"ready": False, "status": f"unreachable ({type(exc).__name__})"}
        return {"dashboard": "ok", "gateway": gateway}

    # ---------- session ----------

    @app.post("/bff/login")
    async def login(request: Request):
        if not csrf_ok(request):
            return _err(403, "csrf", "Missing dashboard request header or cross-origin request.")
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            return _err(429, "rate_limited", "Too many login attempts. Wait a minute and try again.")
        try:
            body = await request.json()
            key = str(body.get("api_key", "")).strip()
        except Exception:  # noqa: BLE001
            return _err(400, "bad_request", "Send JSON: {\"api_key\": \"urumi_...\"}.")
        if not _KEY_RE.match(key):
            return _err(400, "invalid_key_format", "That is not a gateway API key (expected urumi_<id>_<secret>).")
        try:
            r = await state["http"].get("/api/v1/me", headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as exc:
            log.warning("login: gateway unreachable (%s)", type(exc).__name__)
            return _err(503, "gateway_unavailable", "The gateway is not reachable right now.")
        if r.status_code == 401:
            return _err(401, "key_rejected", "This API key is invalid, revoked, or its user is disabled.")
        if r.status_code != 200:
            return _err(502, "gateway_error", f"The gateway returned HTTP {r.status_code}.")
        user = r.json()
        sid = sessions.create(key, user)
        log.info("login user=%s role=%s", user.get("id"), user.get("role"))
        resp = JSONResponse({"user": user})
        set_cookie(resp, sid)
        return resp

    @app.post("/bff/logout")
    async def logout(request: Request):
        if not csrf_ok(request):
            return _err(403, "csrf", "Missing dashboard request header or cross-origin request.")
        sessions.drop(request.cookies.get(COOKIE))
        resp = JSONResponse({"ok": True})
        clear_cookie(resp)
        return resp

    @app.get("/bff/session")
    async def session(request: Request):
        s = sessions.get(request.cookies.get(COOKIE))
        if s is None:
            resp = _err(401, "no_session", "Not signed in or the session expired.")
            clear_cookie(resp)
            return resp
        return {"user": s.user}

    # ---------- chat agent ----------

    def chat_session(request: Request, *, write: bool):
        if write and not csrf_ok(request):
            return None, _err(403, "csrf", "Missing dashboard request header or cross-origin request.")
        sid = request.cookies.get(COOKIE)
        s = sessions.get(sid)
        if s is None:
            resp = _err(401, "no_session", "Not signed in or the session expired.")
            clear_cookie(resp)
            return None, resp
        return (sid, s), None

    async def chat_call(request: Request, fn, *, write: bool = True):
        ctx, err = chat_session(request, write=write)
        if err:
            return err
        sid, s = ctx
        try:
            return JSONResponse(await fn(sid, s))
        except ChatError as exc:
            return _err(exc.status, exc.code, exc.message)
        except GatewayAuthError:
            sessions.drop(sid)
            resp = _err(401, "key_rejected", "Your API key is no longer valid (revoked, or user disabled). Sign in again.")
            clear_cookie(resp)
            return resp

    async def json_body(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise ChatError(413, "too_large", "Request body too large.")
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise ChatError(400, "bad_request", "Send a JSON object.") from None
        if not isinstance(data, dict):
            raise ChatError(400, "bad_request", "Send a JSON object.")
        return data

    def check_action_id(action_id: str) -> None:
        if not _ACTION_ID.match(action_id):
            raise ChatError(404, "no_such_action", "There is no such pending action in this session.")

    @app.get("/bff/chat/status")
    async def chat_status(request: Request):
        return await chat_call(request, lambda sid, s: _async(chat.status()), write=False)

    @app.get("/bff/chat")
    async def chat_state(request: Request):
        return await chat_call(request, lambda sid, s: _async(chat.conversation(sid, s.user["id"]).view()), write=False)

    @app.post("/bff/chat/messages")
    async def chat_send(request: Request):
        async def fn(sid, s):
            body = await json_body(request)
            return await chat.send(sid, s.user["id"], s.key, str(body.get("text", "")))
        return await chat_call(request, fn)

    @app.post("/bff/chat/reset")
    async def chat_reset(request: Request):
        return await chat_call(request, lambda sid, s: _async(chat.reset(sid, s.user["id"])))

    @app.post("/bff/chat/actions/{action_id}/confirm")
    async def chat_confirm(request: Request, action_id: str):
        async def fn(sid, s):
            check_action_id(action_id)
            body = await json_body(request)
            return await chat.confirm(sid, s.user["id"], s.key, action_id, str(body.get("digest", "")))
        return await chat_call(request, fn)

    @app.post("/bff/chat/actions/{action_id}/cancel")
    async def chat_cancel(request: Request, action_id: str):
        async def fn(sid, s):
            check_action_id(action_id)
            return await chat.cancel(sid, s.user["id"], s.key, action_id)
        return await chat_call(request, fn)

    # ---------- gateway proxy ----------

    @app.api_route("/api/v1/{rest:path}", methods=["GET", "POST", "PATCH", "DELETE"])
    async def proxy(request: Request, rest: str):
        path = "/api/v1/" + rest
        method = request.method
        allowed = [m for m, rx in ROUTES if rx.match(path)]
        if not allowed:
            return _err(404, "not_found", "Unknown API route.")
        if method not in allowed:
            return _err(405, "method_not_allowed", "Method not allowed for this route.")
        if method != "GET" and not csrf_ok(request):
            return _err(403, "csrf", "Missing dashboard request header or cross-origin request.")
        sid = request.cookies.get(COOKIE)
        s = sessions.get(sid)
        if s is None:
            resp = _err(401, "no_session", "Not signed in or the session expired.")
            clear_cookie(resp)
            return resp

        params = None
        if path == "/api/v1/audit":
            params = {k: v for k, v in request.query_params.items() if k in AUDIT_PARAMS}
        body = None
        if method in ("POST", "PATCH"):
            body = await request.body()
            if len(body) > MAX_BODY:
                return _err(413, "too_large", "Request body too large.")
        try:
            r = await state["http"].request(
                method, path, params=params, content=body,
                headers={"Authorization": f"Bearer {s.key}", "Content-Type": "application/json"} if body is not None
                else {"Authorization": f"Bearer {s.key}"},
            )
        except httpx.TimeoutException:
            return _err(504, "gateway_timeout", "The gateway did not respond in time.")
        except httpx.HTTPError as exc:
            log.warning("proxy %s %s: gateway unreachable (%s)", method, path, type(exc).__name__)
            return _err(503, "gateway_unavailable", "The gateway is not reachable right now.")

        if r.status_code == 401:
            # Key revoked / user disabled since login: end the session.
            sessions.drop(sid)
            resp = _err(401, "key_rejected", "Your API key is no longer valid (revoked, or user disabled). Sign in again.")
            clear_cookie(resp)
            return resp
        if r.status_code == 204:
            return Response(status_code=204)
        ctype = r.headers.get("content-type", "")
        if "application/json" not in ctype:
            return _err(502, "gateway_error", f"Unexpected gateway response (HTTP {r.status_code}).")
        # Note: never log bodies (a key-creation response contains the raw key once).
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")

    # ---------- SPA ----------

    if (static / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            if full_path.startswith(("api/", "bff/")):
                return _err(404, "not_found", "Unknown route.")
            return FileResponse(static / "index.html", headers={"Cache-Control": "no-store"})

    return app
