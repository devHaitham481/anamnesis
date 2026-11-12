"""FastAPI backend for the memory browser: REST for conversations and search, live updates.

Run with `uvicorn src.web.main:app` or `python -m src.web`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketDisconnect

from src.config import Settings, load_settings
from src.core.db import ThreadNotFoundError
from src.core.memory import MemoryService
from src.core.models import SOURCE_LABELS, Message, SearchHit, Thread

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "testserver"]

ServiceFactory = Callable[[Settings], Awaitable[MemoryService]]


# ---------- request / response schemas ----------


class ThreadUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class Health(BaseModel):
    status: str
    memory: str


# ---------- websocket hub ----------


class Hub:
    """Fan-out of JSON events to every connected browser."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    SEND_TIMEOUT = 5.0

    async def broadcast(self, event: dict[str, Any]) -> None:
        """Send to all clients concurrently; a slow or dead browser can't stall the others."""
        text = json.dumps(event)
        clients = list(self.clients)

        async def send(ws: WebSocket) -> bool:
            try:
                await asyncio.wait_for(ws.send_text(text), self.SEND_TIMEOUT)
                return True
            except Exception:
                return False

        results = await asyncio.gather(*(send(ws) for ws in clients))
        for ws, ok in zip(clients, results, strict=True):
            if not ok:
                self.clients.discard(ws)


def message_event(msg: Message) -> dict[str, Any]:
    return {"type": "message", "message": msg.model_dump()}


async def watch_changes(service: MemoryService, hub: Hub, interval: float) -> None:
    """Poll SQLite for writes from any process (imports, MCP `remember`, other devices, the TUI)
    and push them to browsers."""
    cursor = await service.latest_message_id()
    signature = await service.threads_signature()
    while True:
        await asyncio.sleep(interval)
        try:
            for msg in await service.messages_after(cursor):
                cursor = max(cursor, msg.id)
                await hub.broadcast(message_event(msg))
            new_sig = await service.threads_signature()
            if new_sig != signature:
                signature = new_sig
                await hub.broadcast({"type": "threads_changed"})
        except Exception:
            log.exception("change watcher iteration failed")


def _token_ok(expected: str | None, headers: Any, query: Any) -> bool:
    if expected is None:
        return True
    auth = headers.get("authorization", "")
    supplied = auth[7:] if auth.lower().startswith("bearer ") else query.get("token", "")
    return secrets.compare_digest(supplied.encode(), expected.encode())


def _origin_allowed(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    if origin is None:  # non-browser client (CLI tools, tests)
        return True
    return urlsplit(origin).netloc == ws.headers.get("host")


# ---------- app factory ----------


def create_app(
    settings: Settings | None = None,
    service_factory: ServiceFactory | None = None,
    allowed_hosts: list[str] | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    factory = service_factory or MemoryService.create

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service = await factory(settings)
        hub = Hub()
        app.state.service = service
        app.state.hub = hub
        watcher = asyncio.create_task(watch_changes(service, hub, settings.sync_interval))
        background = [watcher]
        if settings.memory_sync:
            from src.ingest.runner import watch as memory_sync

            background.append(
                asyncio.create_task(memory_sync(service, settings.memory_sync_interval))
            )
        try:
            yield
        finally:
            for task in background:
                task.cancel()
            for task in background:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await service.close()

    app = FastAPI(title="Anamnesis", version="0.1.0", lifespan=lifespan)
    token = settings.auth_token.get_secret_value() if settings.auth_token else None
    hosts = allowed_hosts or [*DEFAULT_ALLOWED_HOSTS, *settings.allowed_hosts]

    @app.middleware("http")
    async def require_token(request: Request, call_next: Any) -> Response:
        if request.url.path.startswith("/api/") and not _token_ok(
            token, request.headers, request.query_params
        ):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        response: Response = await call_next(request)
        return response

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    def svc(request: Request) -> MemoryService:
        return request.app.state.service  # type: ignore[no-any-return]

    async def thread_or_404(service: MemoryService, thread_id: str) -> Thread:
        try:
            return await service.get_thread(thread_id)
        except ThreadNotFoundError:
            raise HTTPException(404, "Thread not found") from None

    # ----- REST -----

    @app.get("/api/health")
    async def health(request: Request) -> Health:
        return Health(status="ok", memory="semantic" if svc(request).index else "keyword")

    @app.get("/api/threads")
    async def list_threads(
        request: Request,
        source: str | None = Query(default=None, max_length=40),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> list[Thread]:
        return await svc(request).list_threads(limit=limit, source=source or None)

    @app.get("/api/sources")
    async def sources(request: Request) -> list[dict[str, Any]]:
        """Which tools the memory holds conversations from (for the source filter)."""
        counts = await svc(request).source_counts()
        return [
            {"name": name, "label": SOURCE_LABELS.get(name, name), "threads": n}
            for name, n in counts.items()
        ]

    @app.get("/api/threads/{thread_id}")
    async def get_thread(request: Request, thread_id: str) -> Thread:
        return await thread_or_404(svc(request), thread_id)

    @app.patch("/api/threads/{thread_id}")
    async def update_thread(request: Request, thread_id: str, body: ThreadUpdate) -> Thread:
        service = svc(request)
        await thread_or_404(service, thread_id)
        return await service.rename_thread(thread_id, body.title)

    @app.delete("/api/threads/{thread_id}", status_code=204)
    async def delete_thread(request: Request, thread_id: str) -> Response:
        service = svc(request)
        await thread_or_404(service, thread_id)
        await service.delete_thread(thread_id)
        return Response(status_code=204)

    @app.get("/api/threads/{thread_id}/messages")
    async def list_messages(
        request: Request, thread_id: str, limit: int | None = Query(default=None, ge=1, le=5000)
    ) -> list[Message]:
        service = svc(request)
        await thread_or_404(service, thread_id)
        return await service.messages(thread_id, limit=limit)

    @app.get("/api/search")
    async def search(
        request: Request,
        q: str = Query(min_length=1, max_length=1000),
        k: int = Query(default=10, ge=1, le=50),
        source: str | None = Query(default=None, max_length=40),
    ) -> list[SearchHit]:
        return await svc(request).search(q, k=k, source=source or None)

    # ----- WebSocket: live updates only (new messages, thread list changes) -----

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        if not _origin_allowed(ws) or not _token_ok(token, ws.headers, ws.query_params):
            await ws.close(code=1008)
            return
        await ws.accept()
        hub: Hub = ws.app.state.hub
        hub.clients.add(ws)
        try:
            while True:
                try:
                    data = json.loads(await ws.receive_text())
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "detail": "Invalid JSON"})
                    continue
                kind = data.get("type") if isinstance(data, dict) else None
                if kind == "ping":
                    await ws.send_json({"type": "pong"})
                else:
                    await ws.send_json({"type": "error", "detail": f"Unknown type {kind!r}"})
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(ws)

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
