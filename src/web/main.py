"""FastAPI backend: REST for threads/messages/search, SSE + WebSocket streaming, live sync.

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
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketDisconnect

from src.config import MissingAPIKeyError, Settings, load_settings
from src.core.chat import (
    AssistantSaved,
    ChatEvent,
    ChatService,
    ThreadBusyError,
    UserSaved,
)
from src.core.db import ThreadNotFoundError
from src.core.models import Message, SearchHit, Thread
from src.providers.router import ProviderError, UnknownModelError

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "testserver"]

ServiceFactory = Callable[[Settings], Awaitable[ChatService]]


# ---------- request / response schemas ----------


class ThreadCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = None


class ThreadUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    model: str | None = None


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)
    model: str | None = None


class ModelInfo(BaseModel):
    name: str
    model: str
    available: bool
    missing_key: str | None = None


class ModelsResponse(BaseModel):
    default: str
    models: list[ModelInfo]


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


async def watch_changes(service: ChatService, hub: Hub, interval: float) -> None:
    """Poll SQLite for writes from any process (e.g. the TUI) and push them to browsers."""
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
    factory = service_factory or ChatService.create

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service = await factory(settings)
        hub = Hub()
        app.state.service = service
        app.state.hub = hub
        watcher = asyncio.create_task(watch_changes(service, hub, settings.sync_interval))
        try:
            yield
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            for task in list(app.state.chat_tasks):
                task.cancel()
            await asyncio.gather(*app.state.chat_tasks, return_exceptions=True)
            await service.close()

    app = FastAPI(title="NexusAI", version="0.1.0", lifespan=lifespan)
    app.state.chat_tasks = set()
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

    def svc(request: Request) -> ChatService:
        return request.app.state.service  # type: ignore[no-any-return]

    async def thread_or_404(service: ChatService, thread_id: str) -> Thread:
        try:
            return await service.get_thread(thread_id)
        except ThreadNotFoundError:
            raise HTTPException(404, "Thread not found") from None

    def validate_model(service: ChatService, model: str | None) -> None:
        if model is None:
            return
        try:
            service.router.resolve(model)
        except UnknownModelError as e:
            raise HTTPException(400, str(e)) from None

    # ----- REST -----

    @app.get("/api/health")
    async def health(request: Request) -> Health:
        return Health(status="ok", memory="semantic" if svc(request).index else "keyword")

    @app.get("/api/models")
    async def models(request: Request) -> ModelsResponse:
        service = svc(request)
        infos = []
        for name, full in service.router.aliases.items():
            missing = service.missing_key(name)
            infos.append(
                ModelInfo(name=name, model=full, available=missing is None, missing_key=missing)
            )
        return ModelsResponse(default=service.default_model, models=infos)

    @app.get("/api/threads")
    async def list_threads(request: Request) -> list[Thread]:
        return await svc(request).list_threads()

    @app.post("/api/threads", status_code=201)
    async def create_thread(request: Request, body: ThreadCreate) -> Thread:
        service = svc(request)
        validate_model(service, body.model)
        return await service.new_thread(model=body.model, title=body.title or "New chat")

    @app.get("/api/threads/{thread_id}")
    async def get_thread(request: Request, thread_id: str) -> Thread:
        return await thread_or_404(svc(request), thread_id)

    @app.patch("/api/threads/{thread_id}")
    async def update_thread(request: Request, thread_id: str, body: ThreadUpdate) -> Thread:
        service = svc(request)
        thread = await thread_or_404(service, thread_id)
        validate_model(service, body.model)
        if body.title is not None:
            thread = await service.rename_thread(thread_id, body.title)
        if body.model is not None and body.model != thread.model:
            thread = await service.set_thread_model(thread_id, body.model)
        return thread

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
    ) -> list[SearchHit]:
        return await svc(request).search(q, k=k)

    async def preflight(service: ChatService, thread_id: str, model: str | None) -> str:
        """Validate a chat turn before streaming; returns the model alias to use."""
        thread = await thread_or_404(service, thread_id)
        model = model or thread.model
        validate_model(service, model)
        missing = service.missing_key(model)
        if missing:
            raise HTTPException(400, f"{missing} is not set (add it to .env and restart)")
        if service.is_busy(thread_id):
            raise HTTPException(409, "This thread is already generating a response")
        return model

    @app.post("/api/threads/{thread_id}/chat")
    async def chat_sse(request: Request, thread_id: str, body: ChatRequest) -> StreamingResponse:
        """Server-Sent Events stream of one chat turn (for curl / non-WebSocket clients)."""
        service = svc(request)
        hub: Hub = request.app.state.hub
        model = await preflight(service, thread_id, body.model)

        async def events() -> AsyncIterator[str]:
            async for payload in run_turn(service, hub, thread_id, body.content, model):
                yield f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    # ----- WebSocket -----

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        if not _origin_allowed(ws) or not _token_ok(token, ws.headers, ws.query_params):
            await ws.close(code=1008)
            return
        await ws.accept()
        hub: Hub = ws.app.state.hub
        service: ChatService = ws.app.state.service
        tasks: set[asyncio.Task[None]] = ws.app.state.chat_tasks
        hub.clients.add(ws)
        # Tell a (re)connecting client which threads are mid-response so it keeps them busy.
        await ws.send_json({"type": "active", "thread_ids": service.active_threads})
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
                elif kind == "send":
                    task = asyncio.create_task(ws_send(service, hub, ws, data))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                else:
                    await ws.send_json({"type": "error", "detail": f"Unknown type {kind!r}"})
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(ws)

    async def ws_send(service: ChatService, hub: Hub, ws: WebSocket, data: dict[str, Any]) -> None:
        thread_id = data.get("thread_id")
        try:
            req = ChatRequest.model_validate(
                {"content": data.get("content", ""), "model": data.get("model")}
            )
            if not isinstance(thread_id, str):
                raise HTTPException(400, "thread_id is required")
            model = await preflight(service, thread_id, req.model)
            async for payload in run_turn(service, hub, thread_id, req.content, model):
                if payload["type"] == "error":  # rejection: only this socket is told
                    await ws.send_json(payload)
                # everything else was already broadcast to all sockets, including this one
        except Exception as e:
            detail = e.detail if isinstance(e, HTTPException) else f"{type(e).__name__}: {e}"
            with contextlib.suppress(Exception):
                await ws.send_json({"type": "error", "thread_id": thread_id, "detail": detail})

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


async def run_turn(
    service: ChatService, hub: Hub, thread_id: str, content: str, model: str
) -> AsyncIterator[dict[str, Any]]:
    """Run a chat turn, broadcast every event to all browsers, and yield the same payloads.

    If the turn is rejected before it starts (busy thread, validation), a single
    `{"type": "error"}` payload is yielded to the caller only — other browsers see nothing,
    so a rejected request can never disturb a turn that is already streaming.
    """
    events = service.send(thread_id, content, model)
    try:
        first = await anext(events)
    except Exception as e:
        await events.aclose()
        if not isinstance(e, MissingAPIKeyError | UnknownModelError | ThreadBusyError | ValueError):
            log.exception("chat turn failed to start")
        detail = str(e) if not isinstance(e, ThreadNotFoundError) else "Thread not found"
        yield {"type": "error", "thread_id": thread_id, "detail": detail}
        return

    start = {"type": "stream_start", "thread_id": thread_id, "model": model}
    await hub.broadcast(start)
    yield start
    error: str | None = None
    try:
        ev: ChatEvent | None = first
        while ev is not None:
            if isinstance(ev, UserSaved | AssistantSaved):
                payload = message_event(ev.message)
            else:
                payload = {"type": "delta", "thread_id": thread_id, "text": ev.text}
            await hub.broadcast(payload)
            yield payload
            ev = await anext(events, None)
    except ProviderError as e:
        log.warning("provider request failed: %s", e)
        error = str(e)
    except Exception as e:
        log.exception("chat turn failed")
        error = f"{type(e).__name__}: {e}"
    finally:
        await events.aclose()
        end: dict[str, Any] = {"type": "stream_end", "thread_id": thread_id, "error": error}
        await hub.broadcast(end)
    yield end


app = create_app()
