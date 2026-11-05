"""Regression tests for issues found in the independent code review."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from textual.widgets import Input

from src.cli.app import NexusApp
from src.config import Settings
from src.core.chat import INTERRUPTED_NOTE, ChatService, Delta, ThreadBusyError
from src.core.db import Database, ThreadNotFoundError
from src.core.models import Message
from src.core.vector import HashEmbedder, VectorIndex
from src.providers.router import ProviderRouter
from src.web.main import create_app
from tests.conftest import SlowLLM


def slow_service(service: ChatService, llm: SlowLLM) -> ChatService:
    return ChatService(
        service.db,
        ProviderRouter(service.settings, completion_fn=llm),
        service.index,
        service.settings,
    )


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
        t = await db.create_thread()
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


# 4. One turn per thread; a second concurrent turn is rejected without side effects.
async def test_second_turn_on_same_thread_is_rejected(service: ChatService) -> None:
    llm = SlowLLM()
    llm.gate = asyncio.Event()
    svc = slow_service(service, llm)
    t = await svc.new_thread()
    first = svc.send(t.id, "one")
    events = [await anext(first), await anext(first)]  # UserSaved + first Delta
    assert svc.is_busy(t.id) and svc.active_threads == [t.id]
    with pytest.raises(ThreadBusyError):
        await anext(svc.send(t.id, "two"))
    llm.gate.set()
    events += [e async for e in first]
    assert not svc.is_busy(t.id)
    assert [m.content for m in await svc.messages(t.id) if m.role == "user"] == ["one"]
    assert "".join(e.text for e in events if isinstance(e, Delta)) == "".join(llm.words)


# Lower priority: cancelling a turn closes the provider stream promptly.
async def test_cancel_closes_provider_stream(service: ChatService) -> None:
    llm = SlowLLM()
    llm.gate = asyncio.Event()
    svc = slow_service(service, llm)
    t = await svc.new_thread()
    gen = svc.send(t.id, "go")
    await anext(gen)
    await anext(gen)
    await gen.aclose()
    assert llm.streams[0].closed
    assert not svc.is_busy(t.id)
    assert (await svc.messages(t.id))[-1].content.endswith(INTERRUPTED_NOTE)


async def test_thread_deleted_mid_stream(service: ChatService) -> None:
    llm = SlowLLM()
    llm.gate = asyncio.Event()
    svc = slow_service(service, llm)
    t = await svc.new_thread()
    gen = svc.send(t.id, "go")
    await anext(gen)
    await anext(gen)
    await service.db.delete_thread(t.id)  # e.g. deleted from the web UI
    llm.gate.set()
    with pytest.raises(ThreadNotFoundError):
        async for _ in gen:
            pass
    assert not service.db.conn.in_transaction
    other = await svc.new_thread()
    await service.db.add_message(other.id, "user", "db still healthy")


# 3. Quitting the TUI mid-stream exits cleanly and keeps the partial reply.
async def test_tui_quit_mid_stream(service: ChatService) -> None:
    llm = SlowLLM()
    llm.gate = asyncio.Event()
    svc = slow_service(service, llm)
    app = NexusApp(service.settings, svc)
    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(100):
            if not app.query_one("#prompt", Input).disabled:
                break
            await pilot.pause(0.02)
        app.query_one("#prompt", Input).value = "long answer please"
        await pilot.press("enter")
        for _ in range(100):  # wait until the provider stream is open (mid-response)
            if llm.streams:
                break
            await pilot.pause(0.02)
        assert llm.streams, "stream never started"
        await pilot.pause(0.1)
        await pilot.press("ctrl+q")
    assert app.return_code in (0, None)
    thread_id = app.thread_id
    assert thread_id is not None
    msgs = await service.messages(thread_id)
    assert msgs[-1].role == "assistant" and msgs[-1].content.endswith(INTERRUPTED_NOTE)


# 4/5. Web: rejected concurrent send only notifies the sender; reconnect gets `active`.
def test_web_busy_thread_and_active_on_connect(settings: Settings) -> None:
    llm = SlowLLM(words=30, delay=0.02)

    async def factory(s: Settings) -> ChatService:
        db = await Database(s.db_path).open()
        return ChatService(
            db, ProviderRouter(s, completion_fn=llm), VectorIndex(s.vector_path, HashEmbedder()), s
        )

    with TestClient(create_app(settings, factory)) as client:
        tid = client.post("/api/threads", json={}).json()["id"]
        with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
            assert a.receive_json() == {"type": "active", "thread_ids": []}
            assert b.receive_json() == {"type": "active", "thread_ids": []}
            a.send_json({"type": "send", "thread_id": tid, "content": "first"})
            assert a.receive_json()["type"] == "stream_start"
            # While streaming: SSE is refused, a second WS send is refused to the sender only,
            # and a newly connecting client learns the thread is busy.
            assert client.post(f"/api/threads/{tid}/chat", json={"content": "x"}).status_code == 409
            b.send_json({"type": "send", "thread_id": tid, "content": "second"})
            with client.websocket_connect("/ws") as c:
                assert c.receive_json() == {"type": "active", "thread_ids": [tid]}
            events_b: list[dict[str, Any]] = []
            while True:
                e = b.receive_json()
                events_b.append(e)
                if e["type"] == "stream_end":
                    break
            errors = [e for e in events_b if e["type"] == "error"]
            assert len(errors) == 1 and "already generating" in errors[0]["detail"]
            assert [e for e in events_b if e["type"] == "stream_end"][0]["error"] is None
            assert sum(e["type"] == "stream_start" for e in events_b) == 1
        msgs = client.get(f"/api/threads/{tid}/messages").json()
        assert [m["content"] for m in msgs if m["role"] == "user"] == ["first"]
