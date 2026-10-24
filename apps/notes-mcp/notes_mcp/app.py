"""Team notes MCP server over Streamable HTTP, backed by SQLite on a volume.

Every MCP request must carry `Authorization: Bearer <NOTES_MCP_TOKEN>`; the
gateway holds that token server-side. Run with:
  uvicorn --factory notes_mcp.app:create_app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

import anyio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AfterValidator, BaseModel, Field

log = logging.getLogger("notes_mcp")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(v: str) -> str:
    v = v.strip()
    if _CTRL.search(v):
        raise ValueError("must not contain control characters")
    if not v:
        raise ValueError("must not be blank")
    return v


Title = Annotated[str, Field(min_length=1, max_length=200, description="Note title (1-200 chars)."), AfterValidator(_clean)]
Body = Annotated[str, Field(min_length=1, max_length=10000, description="Note body, plain text (max 10000 chars)."), AfterValidator(_clean)]
NoteId = Annotated[int, Field(ge=1, le=2**31 - 1, strict=True, description="Note ID.")]
Limit = Annotated[int, Field(ge=1, le=50, strict=True, description="Max notes to return (1-50).")]
Offset = Annotated[int, Field(ge=0, le=100000, strict=True, description="Notes to skip.")]
Query = Annotated[str, Field(min_length=1, max_length=100, description="Case-insensitive text to search in title/body."), AfterValidator(_clean)]


class Note(BaseModel):
    id: int
    title: str
    body: str
    created_at: str


class NoteSummary(BaseModel):
    id: int
    title: str
    created_at: str


class NoteList(BaseModel):
    notes: list[NoteSummary]
    total: int
    limit: int
    offset: int


class Deleted(BaseModel):
    id: int
    deleted: bool


class NotesDB:
    def __init__(self, path: str):
        self.path = path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
                      " body TEXT NOT NULL, created_at TEXT NOT NULL)")

    async def run(self, fn, *args):
        def work():
            with self._conn() as c:
                return fn(c, *args)
        return await anyio.to_thread.run_sync(work)


def build_mcp(db: NotesDB, allowed_hosts: list[str]) -> FastMCP:
    mcp = FastMCP(
        "notes", instructions="Shared team notes: create, list/search, read and delete short text notes.",
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts,
                                                     allowed_origins=[]),
    )

    @mcp.tool(title="Create note", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
    async def create_note(title: Title, body: Body) -> Note:
        """Create a shared team note."""
        now = datetime.now(UTC).isoformat(timespec="seconds")

        def q(c, t, b):
            cur = c.execute("INSERT INTO notes (title, body, created_at) VALUES (?, ?, ?)", (t, b, now))
            return cur.lastrowid
        nid = await db.run(q, title, body)
        return Note(id=nid, title=title, body=body, created_at=now)

    @mcp.tool(title="List notes", annotations=ToolAnnotations(readOnlyHint=True))
    async def list_notes(limit: Limit = 10, offset: Offset = 0, query: Annotated[Query | None, Field(description="Optional search text.")] = None) -> NoteList:
        """List notes, newest first, optionally filtered by a search text."""
        def q(c):
            where, params = "", []
            if query:
                like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                where, params = " WHERE title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\'", [like, like]
            total = c.execute("SELECT count(*) FROM notes" + where, params).fetchone()[0]
            rows = c.execute("SELECT id, title, created_at FROM notes" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                             [*params, limit, offset]).fetchall()
            return total, rows
        total, rows = await db.run(q)
        return NoteList(notes=[NoteSummary(**dict(r)) for r in rows], total=total, limit=limit, offset=offset)

    @mcp.tool(title="Get note", annotations=ToolAnnotations(readOnlyHint=True))
    async def get_note(note_id: NoteId) -> Note:
        """Get one note by ID."""
        row = await db.run(lambda c: c.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone())
        if row is None:
            raise ToolError(f"Note {note_id} not found.")
        return Note(**dict(row))

    @mcp.tool(title="Delete note", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True))
    async def delete_note(note_id: NoteId) -> Deleted:
        """Delete a note permanently."""
        n = await db.run(lambda c: c.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount)
        if not n:
            raise ToolError(f"Note {note_id} not found.")
        return Deleted(id=note_id, deleted=True)

    return mcp


class BearerAuth:
    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = b""
            for k, v in scope.get("headers", []):
                if k == b"authorization" and v[:7].lower() == b"bearer ":
                    got = v[7:].strip()
            if not hmac.compare_digest(got, self.token):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def create_app(*, token: str | None = None, db_path: str | None = None, allowed_hosts: list[str] | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    token = token if token is not None else os.environ.get("NOTES_MCP_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("NOTES_MCP_TOKEN must be set (>= 32 characters)")
    db = NotesDB(db_path or os.environ.get("NOTES_DB_PATH", "/data/notes.db"))
    hosts = allowed_hosts or [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*").split(",") if h.strip()]
    mcp = build_mcp(db, hosts)
    inner = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_):
        db.init()
        async with mcp.session_manager.run():
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return JSONResponse({"status": "ok"})

    app.mount("/", BearerAuth(inner, token))
    return app
