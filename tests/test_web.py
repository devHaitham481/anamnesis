"""Web API tests: browsing, search, live updates over WebSocket, security."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from anamnesis.config import Settings
from anamnesis.core.db import Database
from anamnesis.core.memory import MemoryService
from anamnesis.core.models import ImportedConversation
from anamnesis.web.main import create_app
from tests.conftest import conversation, open_service


def make_client(settings: Settings, **kw: Any) -> TestClient:
    return TestClient(create_app(settings, open_service, **kw))


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with make_client(settings) as c:
        yield c


def seed(client: TestClient, conv: ImportedConversation) -> str:
    service: MemoryService = client.app.state.service  # type: ignore[attr-defined]
    thread, _ = client.portal.call(service.import_conversation, conv)  # type: ignore[union-attr]
    return thread.id


def recv_until(ws: Any, pred: Any, limit: int = 200) -> list[dict[str, Any]]:
    events = []
    for _ in range(limit):
        e = ws.receive_json()
        events.append(e)
        if pred(e):
            return events
    raise AssertionError(f"never matched; got {events}")


def test_static_and_health(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and "Anamnesis" in r.text
    assert "composer" not in r.text  # memory browser only, no chat box
    for f in ("app.js", "markdown.js", "style.css"):
        assert client.get(f"/{f}").status_code == 200
    assert client.get("/api/health").json() == {"status": "ok", "memory": "semantic"}


def test_no_chat_endpoints(client: TestClient) -> None:
    tid = seed(client, conversation("s1", "hello"))
    assert client.get("/api/models").status_code in (404, 405)
    assert client.post("/api/threads", json={}).status_code == 405
    assert client.post(f"/api/threads/{tid}/chat", json={"content": "x"}).status_code in (404, 405)


def test_browse_rename_delete(client: TestClient) -> None:
    tid = seed(
        client,
        conversation("s1", "plan the release", "ship friday", title="Plans", project="/w/app"),
    )
    (t,) = client.get("/api/threads").json()
    assert (t["id"], t["title"], t["source"]) == (tid, "Plans", "claude-code")
    assert t["project"] == "/w/app"
    assert "model" not in t
    msgs = client.get(f"/api/threads/{tid}/messages").json()
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "plan the release"),
        ("assistant", "ship friday"),
    ]
    renamed = client.patch(f"/api/threads/{tid}", json={"title": "Renamed"}).json()
    assert renamed["title"] == "Renamed"
    assert client.patch(f"/api/threads/{tid}", json={"title": ""}).status_code == 422
    assert client.delete(f"/api/threads/{tid}").status_code == 204
    for method, path in [("get", ""), ("delete", ""), ("get", "/messages")]:
        assert getattr(client, method)(f"/api/threads/{tid}{path}").status_code == 404
    assert client.patch("/api/threads/nope", json={"title": "x"}).status_code == 404


def test_search(client: TestClient) -> None:
    tid = seed(client, conversation("s1", "tell me about lancedb vectors", "it is embedded"))
    hits = client.get("/api/search", params={"q": "lancedb"}).json()
    assert hits and hits[0]["thread_id"] == tid and hits[0]["thread_title"]
    assert hits[0]["source"] == "claude-code"
    assert client.get("/api/search", params={"q": ""}).status_code == 422


def test_sources_and_source_filter(client: TestClient) -> None:
    seed(client, conversation("g1", "trip to lisbon ideas", source="chatgpt", title="Lisbon trip"))
    seed(client, conversation("c1", "lisbon office wifi", title="Office"))
    sources = {s["name"]: s for s in client.get("/api/sources").json()}
    assert sources["chatgpt"] == {"name": "chatgpt", "label": "ChatGPT", "threads": 1}
    assert sources["claude-code"]["threads"] == 1
    only = client.get("/api/threads", params={"source": "chatgpt"}).json()
    assert [t["title"] for t in only] == ["Lisbon trip"] and only[0]["source"] == "chatgpt"
    hits = client.get("/api/search", params={"q": "lisbon", "source": "chatgpt"}).json()
    assert hits and all(h["source"] == "chatgpt" for h in hits)
    assert client.get("/api/search", params={"q": "lisbon", "source": "codex"}).json() == []


def test_websocket_ping_and_errors(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        ws.send_text("not json")
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "send", "thread_id": "x", "content": "hi"})  # no chat anymore
        assert ws.receive_json()["type"] == "error"


def test_writes_from_other_processes_are_pushed(client: TestClient, settings: Settings) -> None:
    """Background imports, MCP `remember` or another device write through their own connection."""
    tid = seed(client, conversation("s1", "first message", title="Shared"))

    async def other_process_writes() -> None:
        async with Database(settings.db_path) as other:
            await other.import_conversation(
                conversation("s1", "first message", "a reply that arrived later", title="Shared")
            )
            await other.import_conversation(conversation("s2", "new session", title="Fresh"))

    with client.websocket_connect("/ws") as ws:
        time.sleep(0.1)
        asyncio.run(other_process_writes())
        seen = {"message": False, "threads": False}

        def done(e: dict[str, Any]) -> bool:
            if e["type"] == "message" and e["message"]["content"] == "a reply that arrived later":
                assert e["message"]["thread_id"] == tid
                seen["message"] = True
            seen["threads"] |= e["type"] == "threads_changed"
            return all(seen.values())

        recv_until(ws, done)


def test_rejects_foreign_origin_and_host(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
    assert client.get("/api/threads", headers={"host": "evil.example"}).status_code == 400


def test_auth_token(settings: Settings) -> None:
    s = settings.model_copy(update={"auth_token": SecretStr("s3cret")})
    with make_client(s) as c:
        assert c.get("/").status_code == 200  # static shell is public
        assert c.get("/api/threads").status_code == 401
        assert c.get("/api/threads", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert c.get("/api/threads", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/api/threads?token=s3cret").status_code == 200
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws") as ws:
                ws.receive_json()
        with c.websocket_connect("/ws?token=s3cret") as ws:
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}


def test_module_level_app_imports() -> None:
    from anamnesis.web import main

    assert main.app.title == "Anamnesis"
