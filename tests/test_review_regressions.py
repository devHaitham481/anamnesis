"""Regression tests for issues found in the independent code review."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from src.core.db import Database, ThreadNotFoundError
from src.core.models import Message
from src.core.vector import HashEmbedder, VectorIndex


# 1. Failed write must not leave SQLite locked for the other process.
async def test_failed_write_releases_lock(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    async with Database(path) as tui, Database(path) as web:
        doomed = await tui.create_thread("doomed")
        other = await web.create_thread("other")
        await web.delete_thread(doomed.id)
        with pytest.raises(ThreadNotFoundError):
            await tui.add_message(doomed.id, "user", "into the void")
        assert not tui.conn.in_transaction
        start = time.perf_counter()
        await web.add_message(other.id, "user", "still writable")
        assert time.perf_counter() - start < 1.0
        # The failed connection still sees fresh data (no stale snapshot).
        assert [m.content for m in await tui.get_messages(other.id)] == ["still writable"]


async def test_cancelled_write_rolls_back(tmp_path: Path) -> None:
    async with Database(tmp_path / "c.db") as db:
        t = await db.create_thread("x")
        task = asyncio.create_task(db.add_message(t.id, "user", "x"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not db.conn.in_transaction


# 2. Vector writes from one process are visible to the other, and never duplicated.
def test_vector_cross_handle_visibility_and_idempotency(tmp_path: Path) -> None:
    a = VectorIndex(tmp_path / "v", HashEmbedder())
    b = VectorIndex(tmp_path / "v", HashEmbedder())
    m = Message(id=1, thread_id="t", role="user", content="raspberry cluster", created_at=0)
    a.add([m])
    assert b.count() == 1 and b.search("raspberry cluster")[0].message_id == 1
    b.add([m])
    a.add([m])
    assert a.count() == 1 and b.count() == 1
    assert [h.message_id for h in a.search("raspberry", k=5)] == [1]
