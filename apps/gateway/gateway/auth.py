"""Gateway authentication: `Authorization: Bearer urumi_...` on every request.

The principal is resolved from the gateway's own database. Nothing a client
sends (headers, tool arguments) can choose or override the user identity.
"""

from __future__ import annotations

import json

from fastapi import Depends, HTTPException, Request, status
from starlette.types import ASGIApp, Receive, Scope, Send

from .store import Principal, Store

PRINCIPAL_KEY = "urumi_principal"


def bearer_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    for k, v in headers:
        if k.lower() == b"authorization":
            scheme, _, token = v.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
            return None
    return None


class McpAuthMiddleware:
    """ASGI wrapper for the MCP endpoint: reject unauthenticated requests with 401,
    otherwise attach the principal to the request scope for the MCP handlers."""

    def __init__(self, app: ASGIApp, store: Store):
        self.app = app
        self.store = store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = bearer_token(scope.get("headers", []))
        principal = await self.store.authenticate(token) if token else None
        if principal is None:
            body = json.dumps({"error": "unauthorized", "message": "A valid gateway API key is required."}).encode()
            await send({"type": "http.response.start", "status": 401, "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer realm="urumi-gateway"'),
                (b"content-length", str(len(body)).encode()),
            ]})
            await send({"type": "http.response.body", "body": body})
            return
        scope.setdefault("state", {})[PRINCIPAL_KEY] = principal
        await self.app(scope, receive, send)


def get_store(request: Request) -> Store:
    return request.app.state.store


async def current_principal(request: Request, store: Store = Depends(get_store)) -> Principal:
    token = bearer_token(request.scope.get("headers", []))
    principal = await store.authenticate(token) if token else None
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "A valid gateway API key is required.",
                            headers={"WWW-Authenticate": 'Bearer realm="urumi-gateway"'})
    return principal


async def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This operation requires the admin role.")
    return principal
