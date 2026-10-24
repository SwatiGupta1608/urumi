"""REST API for members and admins (/api/v1). Structured for a future dashboard."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator

from .auth import current_principal, get_store, require_admin
from .ssrf import UnsafeURL, validate_url
from .store import Principal, Store
from .upstreams import CREDENTIAL_REF_RE, SEPARATOR, SLUG_RE, TOOL_NAME_RE, UpstreamManager

router = APIRouter(prefix="/api/v1")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._@-]{0,99}$")

Admin = Annotated[Principal, Depends(require_admin)]
Member = Annotated[Principal, Depends(current_principal)]
StoreDep = Annotated[Store, Depends(get_store)]


def manager(request: Request) -> UpstreamManager:
    return request.app.state.manager


ManagerDep = Annotated[UpstreamManager, Depends(manager)]


# ---------- schemas ----------

class UserOut(BaseModel):
    id: str
    name: str
    role: str
    disabled: bool
    created_at: datetime


class KeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None

    @classmethod
    def of(cls, row: dict) -> "KeyOut":
        return cls(prefix=f"urumi_{row['id']}_…", **{k: row[k] for k in ("id", "name", "created_at", "revoked_at", "last_used_at")})


class NewKeyOut(KeyOut):
    key: str = Field(description="The API key. Shown only in this response; store it now.")


class UserCreate(BaseModel):
    name: str
    role: Literal["admin", "member"] = "member"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not NAME_RE.match(v):
            raise ValueError("name must be 1-100 characters: letters, digits, space, . _ @ -")
        return v


class UserPatch(BaseModel):
    role: Literal["admin", "member"] | None = None
    disabled: bool | None = None


class KeyCreate(BaseModel):
    name: str = "default"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not NAME_RE.match(v):
            raise ValueError("key name must be 1-100 characters: letters, digits, space, . _ @ -")
        return v


def _validate_tools(v: list[str] | None) -> list[str] | None:
    if v is None:
        return v
    if len(v) > 200 or any(not TOOL_NAME_RE.match(t) for t in v):
        raise ValueError("admin_only_tools must be up to 200 upstream tool names")
    return sorted(set(v))


def _validate_display(v: str | None) -> str | None:
    if v is None:
        return v
    v = v.strip()
    if not NAME_RE.match(v):
        raise ValueError("display_name must be 1-100 safe characters")
    return v


def _validate_cred(v: str | None) -> str | None:
    if v is not None and not CREDENTIAL_REF_RE.match(v):
        raise ValueError("credential_ref must match ^[a-z0-9][a-z0-9-]{0,62}$")
    return v


class UpstreamIn(BaseModel):
    slug: str = Field(description="Unique id used as tool prefix: lowercase letters/digits, single hyphens, no '_'.")
    display_name: str
    url: str
    credential_ref: str | None = Field(None, description="Name of a server-side credential file (not the credential).")
    enabled: bool = True
    admin_only_tools: list[str] = Field(default_factory=list, description="Upstream tool names only admins may see/call.")

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v) or SEPARATOR in v or len(v) > 32:
            raise ValueError("slug must match ^[a-z][a-z0-9]*(-[a-z0-9]+)*$ (max 32 chars, no '_')")
        return v

    @field_validator("display_name")
    @classmethod
    def _display(cls, v: str) -> str:
        return _validate_display(v)  # type: ignore[return-value]

    @field_validator("credential_ref")
    @classmethod
    def _cred(cls, v: str | None) -> str | None:
        return _validate_cred(v)

    @field_validator("admin_only_tools")
    @classmethod
    def _tools(cls, v: list[str]) -> list[str]:
        return _validate_tools(v)  # type: ignore[return-value]


class UpstreamPatch(BaseModel):
    display_name: str | None = None
    url: str | None = None
    credential_ref: str | None = None
    clear_credential: bool = False
    enabled: bool | None = None
    admin_only_tools: list[str] | None = None

    @field_validator("display_name")
    @classmethod
    def _display(cls, v: str | None) -> str | None:
        return _validate_display(v)

    @field_validator("credential_ref")
    @classmethod
    def _cred(cls, v: str | None) -> str | None:
        return _validate_cred(v)

    @field_validator("admin_only_tools")
    @classmethod
    def _tools(cls, v: list[str] | None) -> list[str] | None:
        return _validate_tools(v)


class UpstreamOut(BaseModel):
    slug: str
    display_name: str
    url: str
    credential_ref: str | None
    enabled: bool
    admin_only_tools: list[str]
    reachable: bool | None
    last_discovery_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    tool_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, row: dict) -> "UpstreamOut":
        return cls(**{k: row[k] for k in cls.model_fields})


class ToolOut(BaseModel):
    name: str
    upstream: str
    title: str | None
    description: str | None
    admin_only: bool


class AuditOut(BaseModel):
    id: str
    created_at: datetime
    event: str
    user_id: str | None
    upstream_slug: str | None
    tool_name: str | None
    success: bool
    duration_ms: int | None
    error: str | None
    detail: dict | None


# ---------- member endpoints ----------

@router.get("/me", response_model=UserOut, tags=["member"])
async def me(p: Member, store: StoreDep):
    return await store.get_user(p.user_id)


@router.get("/me/keys", response_model=list[KeyOut], tags=["member"])
async def my_keys(p: Member, store: StoreDep):
    return [KeyOut.of(k) for k in await store.list_keys(p.user_id)]


@router.get("/tools", response_model=list[ToolOut], tags=["member"])
async def tools(p: Member, request: Request, mgr: ManagerDep):
    restricted = await request.app.state.admin_only()
    return [
        ToolOut(name=t.name, upstream=t.name.split(SEPARATOR, 1)[0], title=t.title, description=t.description,
                admin_only=t.name.split(SEPARATOR, 1)[1] in restricted.get(t.name.split(SEPARATOR, 1)[0], set()))
        for t in mgr.visible_tools(p, restricted)
    ]


# ---------- admin: users and keys ----------

@router.post("/users", response_model=UserOut, status_code=201, tags=["admin"])
async def create_user(body: UserCreate, p: Admin, store: StoreDep):
    if any(u["name"] == body.name for u in await store.list_users()):
        raise HTTPException(409, "a user with this name already exists")
    user = await store.create_user(body.name, body.role)
    await store.audit(event="admin.user.create", principal=p, success=True, detail={"target_user": user["id"], "role": body.role})
    return user


@router.get("/users", response_model=list[UserOut], tags=["admin"])
async def list_users(p: Admin, store: StoreDep):
    return await store.list_users()


@router.get("/users/{user_id}", response_model=UserOut, tags=["admin"])
async def get_user(user_id: str, p: Admin, store: StoreDep):
    user = await store.get_user(user_id)
    if not user:
        raise HTTPException(404, "user not found")
    return user


@router.patch("/users/{user_id}", response_model=UserOut, tags=["admin"])
async def patch_user(user_id: str, body: UserPatch, p: Admin, store: StoreDep):
    user = await store.get_user(user_id)
    if not user:
        raise HTTPException(404, "user not found")
    values = body.model_dump(exclude_none=True)
    demoting = values.get("role") == "member" or values.get("disabled") is True
    if demoting and user["role"] == "admin" and not user["disabled"] and await store.active_admin_count() <= 1:
        raise HTTPException(409, "cannot demote or disable the last active admin")
    updated = await store.update_user(user_id, **values)
    await store.audit(event="admin.user.update", principal=p, success=True, detail={"target_user": user_id, "fields": sorted(values)})
    return updated


@router.post("/users/{user_id}/keys", response_model=NewKeyOut, status_code=201, tags=["admin"])
async def create_key(user_id: str, body: KeyCreate, p: Admin, store: StoreDep):
    if not await store.get_user(user_id):
        raise HTTPException(404, "user not found")
    key, row = await store.create_key(user_id, body.name)
    await store.audit(event="admin.key.create", principal=p, success=True, detail={"target_user": user_id, "key_id": key.key_id})
    return NewKeyOut(key=key.raw, **KeyOut.of(row).model_dump())


@router.get("/users/{user_id}/keys", response_model=list[KeyOut], tags=["admin"])
async def list_user_keys(user_id: str, p: Admin, store: StoreDep):
    return [KeyOut.of(k) for k in await store.list_keys(user_id)]


@router.delete("/keys/{key_id}", status_code=204, tags=["admin"])
async def revoke_key(key_id: str, p: Admin, store: StoreDep):
    key = await store.get_key(key_id)
    if not key:
        raise HTTPException(404, "key not found")
    await store.revoke_key(key_id)
    await store.audit(event="admin.key.revoke", principal=p, success=True, detail={"target_user": key["user_id"], "key_id": key_id})


# ---------- admin: upstreams ----------

async def _check_url(url: str, request: Request) -> None:
    try:
        await validate_url(url, request.app.state.settings)
    except UnsafeURL as exc:
        raise HTTPException(422, f"upstream URL rejected: {exc}") from None


@router.post("/upstreams", response_model=UpstreamOut, status_code=201, tags=["admin"])
async def create_upstream(body: UpstreamIn, p: Admin, store: StoreDep, mgr: ManagerDep, request: Request):
    if await store.get_upstream(body.slug):
        raise HTTPException(409, f"an upstream with slug '{body.slug}' already exists")
    await _check_url(body.url, request)
    row = await store.create_upstream(**body.model_dump())
    await store.audit(event="admin.upstream.create", principal=p, success=True, upstream_slug=body.slug,
                      detail={"credential_ref_set": body.credential_ref is not None, "enabled": body.enabled})
    if body.enabled:
        await mgr.discover(body.slug)
    return UpstreamOut.of(await store.get_upstream(body.slug) or row)


@router.get("/upstreams", response_model=list[UpstreamOut], tags=["admin"])
async def list_upstreams(p: Admin, store: StoreDep):
    return [UpstreamOut.of(r) for r in await store.list_upstreams()]


@router.get("/upstreams/{slug}", response_model=UpstreamOut, tags=["admin"])
async def get_upstream(slug: str, p: Admin, store: StoreDep):
    row = await store.get_upstream(slug)
    if not row:
        raise HTTPException(404, "upstream not found")
    return UpstreamOut.of(row)


@router.patch("/upstreams/{slug}", response_model=UpstreamOut, tags=["admin"])
async def patch_upstream(slug: str, body: UpstreamPatch, p: Admin, store: StoreDep, mgr: ManagerDep, request: Request):
    row = await store.get_upstream(slug)
    if not row:
        raise HTTPException(404, "upstream not found")
    values = body.model_dump(exclude_none=True, exclude={"clear_credential"})
    if body.clear_credential:
        values["credential_ref"] = None
    if "url" in values:
        await _check_url(values["url"], request)
    await store.update_upstream(slug, **values)
    await store.audit(event="admin.upstream.update", principal=p, success=True, upstream_slug=slug, detail={"fields": sorted(values)})
    # Disabling takes effect immediately; anything else re-discovers.
    await mgr.discover(slug)
    return UpstreamOut.of(await store.get_upstream(slug))  # type: ignore[arg-type]


@router.delete("/upstreams/{slug}", status_code=204, tags=["admin"])
async def delete_upstream(slug: str, p: Admin, store: StoreDep, mgr: ManagerDep):
    if not await store.get_upstream(slug):
        raise HTTPException(404, "upstream not found")
    # Disable first so in-flight calls re-checking state are refused, then remove.
    await store.update_upstream(slug, enabled=False)
    await mgr.remove(slug)
    await store.delete_upstream(slug)
    await store.audit(event="admin.upstream.delete", principal=p, success=True, upstream_slug=slug)


@router.post("/upstreams/{slug}/discover", response_model=UpstreamOut, tags=["admin"])
async def discover_upstream(slug: str, p: Admin, store: StoreDep, mgr: ManagerDep):
    if not await store.get_upstream(slug):
        raise HTTPException(404, "upstream not found")
    result = await mgr.discover(slug)
    await store.audit(event="admin.upstream.discover", principal=p, success=result.ok, upstream_slug=slug,
                      error=None if result.ok else result.error, detail={"tool_count": result.tool_count})
    return UpstreamOut.of(await store.get_upstream(slug))  # type: ignore[arg-type]


# ---------- admin: audit ----------

@router.get("/audit", response_model=list[AuditOut], tags=["admin"])
async def audit(
    p: Admin, store: StoreDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    user_id: str | None = None, upstream: str | None = None,
    success: bool | None = None, event: str | None = None,
):
    return await store.list_audit(limit=limit, user_id=user_id, upstream_slug=upstream, success=success, event=event)


@router.get("/admin/status", tags=["admin"], status_code=status.HTTP_200_OK)
async def admin_status(p: Admin, store: StoreDep, mgr: ManagerDep):
    ups = await store.list_upstreams()
    return {"upstreams": len(ups), "enabled": sum(1 for u in ups if u["enabled"]),
            "registered_tools": len(mgr.visible_tools(p, {}))}
