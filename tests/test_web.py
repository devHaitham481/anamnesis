"""Web API tests: REST, SSE, WebSocket streaming/broadcast, cross-process sync, security."""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from src.config import Settings
from src.core.chat import ChatService
from src.core.db import Database
from src.core.vector import HashEmbedder, VectorIndex
from src.providers.router import ProviderRouter
from src.web.main import create_app
from tests.conftest import FakeLLM


def make_client(settings: Settings, llm: FakeLLM, **kw: Any) -> TestClient:
    async def factory(s: Settings) -> ChatService:
        db = await Database(s.db_path).open()
        idx = VectorIndex(s.vector_path, HashEmbedder())
        return ChatService(db, ProviderRouter(s, completion_fn=llm), idx, s)

    return TestClient(create_app(settings, factory, **kw))


@pytest.fixture
def client(settings: Settings, fake_llm: FakeLLM) -> Iterator[TestClient]:
    with make_client(settings, fake_llm) as c:
        yield c


@contextlib.contextmanager
def ws_open(client: TestClient, url: str = "/ws", **kw: Any) -> Iterator[Any]:
    """Connect and consume the initial `active` event every client receives."""
    with client.websocket_connect(url, **kw) as ws:
        first = ws.receive_json()
        assert first["type"] == "active" and isinstance(first["thread_ids"], list)
        yield ws


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
    for f in ("app.js", "markdown.js", "style.css"):
        assert client.get(f"/{f}").status_code == 200
    assert client.get("/api/health").json() == {"status": "ok", "memory": "semantic"}


def test_models(client: TestClient, settings: Settings) -> None:
    body = client.get("/api/models").json()
    assert body["default"] == "claude"
    assert {m["name"]: m["available"] for m in body["models"]} == {"claude": True, "gemini": True}
    assert body["models"][0]["model"] == settings.claude_model


def test_models_reports_missing_key(settings: Settings, fake_llm: FakeLLM) -> None:
    s = settings.model_copy(update={"gemini_api_key": None})
    with make_client(s, fake_llm) as c:
        gem = [m for m in c.get("/api/models").json()["models"] if m["name"] == "gemini"][0]
        assert gem == {
            "name": "gemini",
            "model": s.gemini_model,
            "available": False,
            "missing_key": "GEMINI_API_KEY",
        }
        tid = c.post("/api/threads", json={"model": "gemini"}).json()["id"]
        r = c.post(f"/api/threads/{tid}/chat", json={"content": "hi"})
        assert r.status_code == 400 and "GEMINI_API_KEY" in r.json()["detail"]


def test_thread_crud(client: TestClient) -> None:
    r = client.post("/api/threads", json={"title": "Plans", "model": "gemini"})
    assert r.status_code == 201
    t = r.json()
    assert t["title"] == "Plans" and t["model"] == "gemini"
    assert [x["id"] for x in client.get("/api/threads").json()] == [t["id"]]
    assert (
        client.patch(f"/api/threads/{t['id']}", json={"title": "Renamed"}).json()["title"]
        == "Renamed"
    )
    assert (
        client.patch(f"/api/threads/{t['id']}", json={"model": "claude"}).json()["model"]
        == "claude"
    )
    assert client.patch(f"/api/threads/{t['id']}", json={"model": "gpt-9"}).status_code == 400
    assert client.post("/api/threads", json={"model": "gpt-9"}).status_code == 400
    assert client.get(f"/api/threads/{t['id']}/messages").json() == []
    assert client.delete(f"/api/threads/{t['id']}").status_code == 204
    for method, path in [("get", ""), ("delete", ""), ("get", "/messages")]:
        assert getattr(client, method)(f"/api/threads/{t['id']}{path}").status_code == 404
    assert client.patch("/api/threads/nope", json={"title": "x"}).status_code == 404


def test_sse_chat_and_search(client: TestClient, fake_llm: FakeLLM) -> None:
    tid = client.post("/api/threads", json={}).json()["id"]
    with client.stream(
        "POST", f"/api/threads/{tid}/chat", json={"content": "tell me about lancedb vectors"}
    ) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "stream_start" and kinds[-1] == "stream_end"
    assert events[-1]["error"] is None
    assert "".join(e["text"] for e in events if e["type"] == "delta") == fake_llm.reply
    msgs = client.get(f"/api/threads/{tid}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert client.get(f"/api/threads/{tid}").json()["title"] == "tell me about lancedb vectors"
    hits = client.get("/api/search", params={"q": "lancedb"}).json()
    assert hits and hits[0]["thread_id"] == tid and hits[0]["thread_title"]
    assert client.get("/api/search", params={"q": ""}).status_code == 422
    assert client.post(f"/api/threads/{tid}/chat", json={"content": ""}).status_code == 422
    assert client.post("/api/threads/nope/chat", json={"content": "x"}).status_code == 404


def test_websocket_chat_broadcasts_to_all_clients(client: TestClient, fake_llm: FakeLLM) -> None:
    tid = client.post("/api/threads", json={}).json()["id"]
    with ws_open(client) as a, ws_open(client) as b:
        a.send_json({"type": "ping"})
        assert a.receive_json() == {"type": "pong"}
        a.send_json({"type": "send", "thread_id": tid, "content": "hello", "model": "gemini"})
        for ws in (a, b):
            events = recv_until(ws, lambda e: e["type"] == "stream_end")
            kinds = [e["type"] for e in events]
            assert kinds[0] == "stream_start" and "delta" in kinds
            saved = [e["message"] for e in events if e["type"] == "message"]
            assert [m["role"] for m in saved] == ["user", "assistant"]
            assert saved[1]["provider"] == "gemini" and saved[1]["content"] == fake_llm.reply
    assert fake_llm.calls[-1]["model"].startswith("gemini/")


def test_websocket_errors(client: TestClient, fake_llm: FakeLLM) -> None:
    tid = client.post("/api/threads", json={}).json()["id"]
    with ws_open(client) as ws:
        ws.send_text("not json")
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "bogus"})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "send", "thread_id": "missing", "content": "x"})
        assert ws.receive_json() == {
            "type": "error",
            "thread_id": "missing",
            "detail": "Thread not found",
        }
        ws.send_json({"type": "send", "thread_id": tid, "content": "x", "model": "gpt-9"})
        e = ws.receive_json()
        assert e["type"] == "error" and "Unsupported model" in e["detail"]
        fake_llm.fail = RuntimeError("upstream 529 overloaded")
        ws.send_json({"type": "send", "thread_id": tid, "content": "x"})
        end = recv_until(ws, lambda e: e["type"] == "stream_end")[-1]
        assert "overloaded" in end["error"]


def test_writes_from_other_process_are_pushed(client: TestClient, settings: Settings) -> None:
    """Simulates the TUI writing to the same SQLite file through its own connection."""
    import asyncio

    tid = client.post("/api/threads", json={"title": "Shared"}).json()["id"]

    async def tui_writes() -> None:
        async with Database(settings.db_path) as other:
            await other.add_message(tid, "user", "typed in the terminal")
            await other.create_thread("Made in TUI")

    with ws_open(client) as ws:
        time.sleep(0.1)
        asyncio.run(tui_writes())
        seen = {"message": False, "threads": False}

        def done(e: dict[str, Any]) -> bool:
            if e["type"] == "message" and e["message"]["content"] == "typed in the terminal":
                assert e["message"]["thread_id"] == tid
                seen["message"] = True
            seen["threads"] |= e["type"] == "threads_changed"
            return all(seen.values())

        recv_until(ws, done)


def test_rejects_foreign_origin_and_host(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    with ws_open(client, headers={"origin": "http://testserver"}) as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
    assert client.get("/api/threads", headers={"host": "evil.example"}).status_code == 400


def test_auth_token(settings: Settings, fake_llm: FakeLLM) -> None:
    s = settings.model_copy(update={"auth_token": SecretStr("s3cret")})
    with make_client(s, fake_llm) as c:
        assert c.get("/").status_code == 200  # static shell is public
        assert c.get("/api/threads").status_code == 401
        assert c.get("/api/threads", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert c.get("/api/threads", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/api/threads?token=s3cret").status_code == 200
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws") as ws:
                ws.receive_json()
        with ws_open(c, "/ws?token=s3cret") as ws:
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}


def test_module_level_app_imports() -> None:
    from src.web import main

    assert main.app.title == "Anamnesis"


def test_sources_and_source_filter(client: TestClient) -> None:
    from src.core.models import ImportedConversation, ImportedMessage

    service = client.app.state.service  # type: ignore[attr-defined]
    conv = ImportedConversation(
        source="chatgpt",
        external_id="g1",
        title="Lisbon trip",
        messages=[
            ImportedMessage(
                external_id="1", role="user", content="trip to lisbon ideas", created_at=1.0
            )
        ],
    )
    client.portal.call(service.import_conversation, conv)  # type: ignore[attr-defined]
    client.post("/api/threads", json={})
    sources = {s["name"]: s for s in client.get("/api/sources").json()}
    assert sources["chatgpt"] == {"name": "chatgpt", "label": "ChatGPT", "threads": 1}
    assert sources["anamnesis"]["threads"] == 1
    only = client.get("/api/threads", params={"source": "chatgpt"}).json()
    assert [t["title"] for t in only] == ["Lisbon trip"] and only[0]["source"] == "chatgpt"
    hits = client.get("/api/search", params={"q": "lisbon", "source": "chatgpt"}).json()
    assert hits and all(h["source"] == "chatgpt" for h in hits)
    assert client.get("/api/search", params={"q": "lisbon", "source": "codex"}).json() == []
