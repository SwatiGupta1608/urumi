"""Data access for users, keys, upstreams and audit records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from . import db
from .security import NewKey, generate_key, key_matches, parse_key, sanitize

ROLES = ("admin", "member")


@dataclass(frozen=True)
class Principal:
    user_id: str
    name: str
    role: str
    key_id: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class Store:
    def __init__(self, engine: AsyncEngine, pepper: bytes):
        self.engine = engine
        self._pepper = pepper

    # ---------- authentication ----------

    async def authenticate(self, raw_key: str) -> Principal | None:
        """Resolve a raw API key to an active principal. Checked on every request,
        so revocation and user disablement take effect immediately."""
        key_id = parse_key(raw_key)
        if key_id is None:
            return None
        q = (
            sa.select(db.api_keys.c.key_hash, db.api_keys.c.revoked_at, db.users.c.id,
                      db.users.c.name, db.users.c.role, db.users.c.disabled)
            .join(db.users, db.users.c.id == db.api_keys.c.user_id)
            .where(db.api_keys.c.id == key_id)
        )
        async with self.engine.connect() as conn:
            row = (await conn.execute(q)).first()
        if row is None:
            # Burn the same HMAC work so unknown ids are not trivially distinguishable.
            key_matches(self._pepper, raw_key, "0" * 64)
            return None
        if not key_matches(self._pepper, raw_key, row.key_hash):
            return None
        if row.revoked_at is not None or row.disabled:
            return None
        async with self.engine.begin() as conn:
            await conn.execute(db.api_keys.update().where(db.api_keys.c.id == key_id).values(last_used_at=db.utcnow()))
        return Principal(user_id=row.id, name=row.name, role=row.role, key_id=key_id)

    # ---------- users ----------

    async def create_user(self, name: str, role: str) -> dict:
        assert role in ROLES
        uid = db.new_id()
        async with self.engine.begin() as conn:
            await conn.execute(db.users.insert().values(id=uid, name=name, role=role, disabled=False, created_at=db.utcnow()))
        return await self.get_user(uid)  # type: ignore[return-value]

    async def get_user(self, user_id: str) -> dict | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(sa.select(db.users).where(db.users.c.id == user_id))).first()
        return dict(row._mapping) if row else None

    async def list_users(self) -> list[dict]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(sa.select(db.users).order_by(db.users.c.created_at))).all()
        return [dict(r._mapping) for r in rows]

    async def update_user(self, user_id: str, **values: Any) -> dict | None:
        if values:
            async with self.engine.begin() as conn:
                await conn.execute(db.users.update().where(db.users.c.id == user_id).values(**values))
        return await self.get_user(user_id)

    async def active_admin_count(self) -> int:
        q = sa.select(sa.func.count()).select_from(db.users).where(db.users.c.role == "admin", db.users.c.disabled.is_(False))
        async with self.engine.connect() as conn:
            return int((await conn.execute(q)).scalar_one())

    # ---------- keys ----------

    async def create_key(self, user_id: str, name: str) -> tuple[NewKey, dict]:
        key = generate_key(self._pepper)
        async with self.engine.begin() as conn:
            await conn.execute(db.api_keys.insert().values(
                id=key.key_id, user_id=user_id, name=name, key_hash=key.key_hash, created_at=db.utcnow()))
        return key, (await self.get_key(key.key_id))  # type: ignore[return-value]

    async def get_key(self, key_id: str) -> dict | None:
        cols = [c for c in db.api_keys.c if c.name != "key_hash"]
        async with self.engine.connect() as conn:
            row = (await conn.execute(sa.select(*cols).where(db.api_keys.c.id == key_id))).first()
        return dict(row._mapping) if row else None

    async def list_keys(self, user_id: str) -> list[dict]:
        cols = [c for c in db.api_keys.c if c.name != "key_hash"]
        q = sa.select(*cols).where(db.api_keys.c.user_id == user_id).order_by(db.api_keys.c.created_at)
        async with self.engine.connect() as conn:
            return [dict(r._mapping) for r in (await conn.execute(q)).all()]

    async def revoke_key(self, key_id: str) -> bool:
        async with self.engine.begin() as conn:
            res = await conn.execute(
                db.api_keys.update()
                .where(db.api_keys.c.id == key_id, db.api_keys.c.revoked_at.is_(None))
                .values(revoked_at=db.utcnow())
            )
        return res.rowcount > 0

    # ---------- upstreams ----------

    async def list_upstreams(self) -> list[dict]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(sa.select(db.upstreams).order_by(db.upstreams.c.slug))).all()
        return [dict(r._mapping) for r in rows]

    async def get_upstream(self, slug: str) -> dict | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(sa.select(db.upstreams).where(db.upstreams.c.slug == slug))).first()
        return dict(row._mapping) if row else None

    async def create_upstream(self, **values: Any) -> dict:
        now = db.utcnow()
        async with self.engine.begin() as conn:
            await conn.execute(db.upstreams.insert().values(id=db.new_id(), created_at=now, updated_at=now, tool_count=0, **values))
        return await self.get_upstream(values["slug"])  # type: ignore[return-value]

    async def update_upstream(self, slug: str, **values: Any) -> dict | None:
        if values:
            async with self.engine.begin() as conn:
                await conn.execute(db.upstreams.update().where(db.upstreams.c.slug == slug).values(updated_at=db.utcnow(), **values))
        return await self.get_upstream(slug)

    async def delete_upstream(self, slug: str) -> bool:
        async with self.engine.begin() as conn:
            res = await conn.execute(db.upstreams.delete().where(db.upstreams.c.slug == slug))
        return res.rowcount > 0

    async def record_health(self, slug: str, *, ok: bool, error: str | None, tool_count: int | None) -> None:
        now = db.utcnow()
        values: dict[str, Any] = {"reachable": ok, "last_discovery_at": now, "last_error": sanitize(error, 500) if error else None}
        if ok:
            values["last_success_at"] = now
        if tool_count is not None:
            values["tool_count"] = tool_count
        async with self.engine.begin() as conn:
            await conn.execute(db.upstreams.update().where(db.upstreams.c.slug == slug).values(**values))

    # ---------- audit ----------

    async def audit(self, *, event: str, principal: Principal | None, success: bool,
                    upstream_slug: str | None = None, tool_name: str | None = None,
                    duration_ms: int | None = None, error: str | None = None,
                    detail: dict | None = None) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(db.audit_log.insert().values(
                id=db.new_id(), created_at=db.utcnow(), event=event,
                user_id=principal.user_id if principal else None,
                upstream_slug=upstream_slug, tool_name=tool_name, success=success,
                duration_ms=duration_ms, error=sanitize(error, 500) if error else None, detail=detail,
            ))

    async def list_audit(self, *, limit: int, user_id: str | None = None, upstream_slug: str | None = None,
                         success: bool | None = None, event: str | None = None) -> list[dict]:
        q = sa.select(db.audit_log).order_by(db.audit_log.c.created_at.desc()).limit(limit)
        if user_id:
            q = q.where(db.audit_log.c.user_id == user_id)
        if upstream_slug:
            q = q.where(db.audit_log.c.upstream_slug == upstream_slug)
        if success is not None:
            q = q.where(db.audit_log.c.success.is_(success))
        if event:
            q = q.where(db.audit_log.c.event == event)
        async with self.engine.connect() as conn:
            return [dict(r._mapping) for r in (await conn.execute(q)).all()]
