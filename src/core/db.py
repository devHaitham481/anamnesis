"""Async SQLite store for threads, messages and providers (with FTS5 keyword search)."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType
from typing import Any

import aiosqlite

from src.core.models import Message, Role, SearchHit, Thread

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    name          TEXT PRIMARY KEY,
    default_model TEXT NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS threads (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    model         TEXT NOT NULL,
    system_prompt TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
    content    TEXT NOT NULL,
    provider   TEXT,
    model      TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, id);
CREATE INDEX IF NOT EXISTS idx_threads_updated ON threads(updated_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(content, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

_THREAD_COLS = "id, title, model, system_prompt, created_at, updated_at"
_MSG_COLS = "id, thread_id, role, content, provider, model, created_at"


class ThreadNotFoundError(LookupError):
    pass


def _fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query: quoted tokens, OR-ed, prefix-matched."""
    tokens = re.findall(r"\w+", text)
    if not tokens:
        return None
    return " OR ".join(f'"{t}"*' for t in tokens)


class Database:
    """Thin async repository over SQLite. Use as `async with Database(path) as db:`."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> Database:
        if self._conn is None:
            if str(self.path) != ":memory:":
                self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(self.path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            await self._conn.execute("PRAGMA busy_timeout=5000")
            await self._conn.executescript(SCHEMA)
            await self._conn.commit()
        return self

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> Database:
        return await self.open()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not open")
        return self._conn

    @contextlib.asynccontextmanager
    async def _write(self) -> AsyncIterator[aiosqlite.Connection]:
        """One write transaction: commit on success, ALWAYS roll back on failure/cancellation.

        Without the rollback a failed statement leaves sqlite3's implicit transaction open,
        holding the WAL write lock and blocking every other process (TUI <-> web server).
        """
        conn = self.conn
        try:
            yield conn
            await conn.commit()
        except BaseException:
            await asyncio.shield(conn.rollback())
            raise

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    # ---------- providers ----------

    async def upsert_provider(self, name: str, default_model: str) -> None:
        async with self._write() as conn:
            await conn.execute(
                "INSERT INTO providers(name, default_model, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET default_model=excluded.default_model, "
                "updated_at=excluded.updated_at",
                (name, default_model, time.time()),
            )

    async def list_providers(self) -> dict[str, str]:
        rows = await self._fetchall("SELECT name, default_model FROM providers ORDER BY name")
        return {r["name"]: r["default_model"] for r in rows}

    # ---------- threads ----------

    async def create_thread(
        self, title: str = "New chat", model: str = "claude", system_prompt: str | None = None
    ) -> Thread:
        now = time.time()
        thread = Thread(
            id=uuid.uuid4().hex,
            title=title,
            model=model,
            system_prompt=system_prompt,
            created_at=now,
            updated_at=now,
        )
        async with self._write() as conn:
            await conn.execute(
                f"INSERT INTO threads({_THREAD_COLS}) VALUES (?, ?, ?, ?, ?, ?)",
                (thread.id, thread.title, thread.model, thread.system_prompt, now, now),
            )
        return thread

    async def get_thread(self, thread_id: str) -> Thread:
        rows = await self._fetchall(
            f"SELECT {_THREAD_COLS} FROM threads WHERE id = ?", (thread_id,)
        )
        if not rows:
            raise ThreadNotFoundError(thread_id)
        return Thread(**dict(rows[0]))

    async def list_threads(self, limit: int = 200) -> list[Thread]:
        rows = await self._fetchall(
            f"SELECT {_THREAD_COLS} FROM threads ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        return [Thread(**dict(r)) for r in rows]

    async def update_thread(
        self, thread_id: str, *, title: str | None = None, model: str | None = None
    ) -> Thread:
        thread = await self.get_thread(thread_id)
        async with self._write() as conn:
            await conn.execute(
                "UPDATE threads SET title = ?, model = ?, updated_at = ? WHERE id = ?",
                (title or thread.title, model or thread.model, time.time(), thread_id),
            )
        return await self.get_thread(thread_id)

    async def delete_thread(self, thread_id: str) -> None:
        async with self._write() as conn:
            cur = await conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
        if cur.rowcount == 0:
            raise ThreadNotFoundError(thread_id)

    async def threads_signature(self) -> tuple[int, float, int]:
        """Cheap change-detector for the thread list: (count, max updated_at, total changes)."""
        rows = await self._fetchall(
            "SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), 0) AS u, "
            "COALESCE(SUM(LENGTH(title)), 0) AS t FROM threads"
        )
        r = rows[0]
        return int(r["n"]), float(r["u"]), int(r["t"])

    # ---------- messages ----------

    async def add_message(
        self,
        thread_id: str,
        role: Role,
        content: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> Message:
        now = time.time()
        try:
            async with self._write() as conn:
                cur = await conn.execute(
                    "INSERT INTO messages(thread_id, role, content, provider, model, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (thread_id, role, content, provider, model, now),
                )
                await conn.execute(
                    "UPDATE threads SET updated_at = ? WHERE id = ?", (now, thread_id)
                )
        except aiosqlite.IntegrityError as e:
            raise ThreadNotFoundError(thread_id) from e
        assert cur.lastrowid is not None
        return Message(
            id=cur.lastrowid,
            thread_id=thread_id,
            role=role,
            content=content,
            provider=provider,
            model=model,
            created_at=now,
        )

    async def get_messages(self, thread_id: str, limit: int | None = None) -> list[Message]:
        """Messages in chronological order; with `limit`, only the most recent `limit`."""
        if limit is None:
            rows = await self._fetchall(
                f"SELECT {_MSG_COLS} FROM messages WHERE thread_id = ? ORDER BY id", (thread_id,)
            )
        else:
            rows = await self._fetchall(
                f"SELECT * FROM (SELECT {_MSG_COLS} FROM messages WHERE thread_id = ? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id",
                (thread_id, limit),
            )
        return [Message(**dict(r)) for r in rows]

    async def get_messages_by_ids(self, ids: list[int]) -> list[Message]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = await self._fetchall(
            f"SELECT {_MSG_COLS} FROM messages WHERE id IN ({marks}) ORDER BY id", tuple(ids)
        )
        return [Message(**dict(r)) for r in rows]

    async def messages_after(self, after_id: int, limit: int = 500) -> list[Message]:
        rows = await self._fetchall(
            f"SELECT {_MSG_COLS} FROM messages WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)
        )
        return [Message(**dict(r)) for r in rows]

    async def max_message_id(self) -> int:
        rows = await self._fetchall("SELECT COALESCE(MAX(id), 0) AS m FROM messages")
        return int(rows[0]["m"])

    async def all_messages(self) -> list[Message]:
        rows = await self._fetchall(f"SELECT {_MSG_COLS} FROM messages ORDER BY id")
        return [Message(**dict(r)) for r in rows]

    async def search_messages(self, query: str, limit: int = 10) -> list[SearchHit]:
        """Keyword search via FTS5 (bm25-ranked)."""
        fts = _fts_query(query)
        if fts is None:
            return []
        rows = await self._fetchall(
            "SELECT m.id, m.thread_id, m.role, m.content, t.title, bm25(messages_fts) AS rank "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            "JOIN threads t ON t.id = m.thread_id "
            "WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts, limit),
        )
        return [
            SearchHit(
                message_id=r["id"],
                thread_id=r["thread_id"],
                role=r["role"],
                content=r["content"],
                score=-float(r["rank"]),
                thread_title=r["title"],
            )
            for r in rows
        ]
