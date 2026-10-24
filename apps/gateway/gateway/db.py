"""Persistence: users, API-key hashes, upstream registrations, audit records.

SQLAlchemy Core with an async engine (asyncpg in production, aiosqlite in
tests). The schema is created idempotently at startup; there is no migration
tool yet (see README).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

metadata = sa.MetaData()


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


users = sa.Table(
    "users", metadata,
    sa.Column("id", sa.String(32), primary_key=True, default=new_id),
    sa.Column("name", sa.String(100), nullable=False, unique=True),
    sa.Column("role", sa.String(16), nullable=False),  # admin | member
    sa.Column("disabled", sa.Boolean, nullable=False, default=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, default=utcnow),
    sa.CheckConstraint("role in ('admin','member')", name="ck_users_role"),
)

api_keys = sa.Table(
    "api_keys", metadata,
    # Public identifier embedded in the key ("urumi_<id>_<secret>"); used for lookup.
    sa.Column("id", sa.String(16), primary_key=True),
    sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("name", sa.String(100), nullable=False),
    # HMAC-SHA256(pepper, full key), hex. The raw key is never stored.
    sa.Column("key_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, default=utcnow),
    sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
)

upstreams = sa.Table(
    "upstreams", metadata,
    sa.Column("id", sa.String(32), primary_key=True, default=new_id),
    sa.Column("slug", sa.String(32), nullable=False, unique=True),
    sa.Column("display_name", sa.String(100), nullable=False),
    sa.Column("url", sa.String(500), nullable=False),
    # Name of a server-side credential file; never the credential itself.
    sa.Column("credential_ref", sa.String(63), nullable=True),
    sa.Column("enabled", sa.Boolean, nullable=False, default=True),
    # Upstream tool names (un-namespaced) only admins may see and call.
    sa.Column("admin_only_tools", sa.JSON, nullable=False, default=list),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, default=utcnow),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow),
    # Health, written by discovery.
    sa.Column("reachable", sa.Boolean, nullable=True),
    sa.Column("last_discovery_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_error", sa.String(500), nullable=True),
    sa.Column("tool_count", sa.Integer, nullable=False, default=0),
)

audit_log = sa.Table(
    "audit_log", metadata,
    sa.Column("id", sa.String(32), primary_key=True, default=new_id),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, default=utcnow, index=True),
    # tool_call | admin.* | auth.denied
    sa.Column("event", sa.String(40), nullable=False),
    # Always the authenticated principal; never taken from client input.
    sa.Column("user_id", sa.String(32), nullable=True, index=True),
    sa.Column("upstream_slug", sa.String(32), nullable=True),
    sa.Column("tool_name", sa.String(160), nullable=True),
    sa.Column("success", sa.Boolean, nullable=False),
    sa.Column("duration_ms", sa.Integer, nullable=True),
    sa.Column("error", sa.String(500), nullable=True),
    # Small, non-sensitive context (target ids, changed field names). No tool arguments/results.
    sa.Column("detail", sa.JSON, nullable=True),
)


def make_engine(url: str) -> AsyncEngine:
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("postgresql"):
        kwargs.update(pool_size=5, max_overflow=5, pool_timeout=5,
                      connect_args={"timeout": 5, "command_timeout": 10})
    return create_async_engine(url, **kwargs)


async def init_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
