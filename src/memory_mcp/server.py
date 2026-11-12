"""Stdio MCP server: lets any MCP-capable agent search and add to the shared memory.

Started by the client (e.g. `claude mcp add anamnesis -- anam-mcp`). While it runs it also keeps
importing new history from the other tools in the background, so memory stays fresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import AsyncIterator
from typing import Any

from mcp.server.mcpserver import MCPServer

from src.config import Settings, load_settings
from src.core.memory import MemoryService
from src.core.models import SOURCE_LABELS, SearchHit, Thread

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Anamnesis is the user's shared, local memory of past conversations across all their AI tools
(Claude Code, Codex, Gemini CLI, Cursor, ChatGPT, claude.ai, Gemini, ...).
- Call `search_memory` when the user refers to earlier work, decisions, preferences or anything
  "we discussed", or when starting a task where prior context could help. Search before asking
  the user to re-explain.
- Call `remember` to save durable facts, decisions and preferences worth recalling later.
- Results come from other sessions and may be outdated; prefer the current repo state if they
  conflict."""

SNIPPET_CHARS = 700


_state: dict[str, Any] = {}


def _service() -> MemoryService:
    service = _state.get("service")
    if service is None:
        raise RuntimeError("Anamnesis memory is still starting up; try again in a moment.")
    return service  # type: ignore[no-any-return]


def _when(ts: float | None) -> str:
    if not ts:
        return "unknown date"
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _label(source: str | None) -> str:
    return SOURCE_LABELS.get(source or "", source or "unknown")


def _snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def format_hits(hits: list[SearchHit]) -> str:
    if not hits:
        return "No matching memories."
    out = []
    for i, h in enumerate(hits, 1):
        where = f" · {h.project}" if h.project else ""
        out.append(
            f"[{i}] {_label(h.source)}{where} · {_when(h.created_at)} · "
            f'"{h.thread_title or "Untitled"}" (thread {h.thread_id})\n'
            f"{h.role}: {_snippet(h.content)}"
        )
    return "\n\n".join(out)


def format_threads(threads: list[Thread]) -> str:
    if not threads:
        return "No conversations stored yet."
    return "\n".join(
        f"- {t.id} · {_label(t.source)} · {_when(t.updated_at)} · {t.title}"
        + (f" · {t.project}" if t.project else "")
        for t in threads
    )


def build_server(
    settings: Settings | None = None,
    service: MemoryService | None = None,
    sync: bool | None = None,
) -> MCPServer[Any]:
    """Create the MCP server. Inject `service` in tests; otherwise it is opened at startup."""

    @contextlib.asynccontextmanager
    async def lifespan(_: MCPServer[Any]) -> AsyncIterator[None]:
        own = service is None
        svc = service or await MemoryService.create(settings or load_settings())
        _state["service"] = svc
        stop = asyncio.Event()
        task = None
        if svc.settings.memory_sync if sync is None else sync:
            from src.ingest.runner import watch

            task = asyncio.create_task(
                watch(svc, interval=svc.settings.memory_sync_interval, stop=stop)
            )
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            _state.pop("service", None)
            if own:
                await svc.close()

    server: MCPServer[Any] = MCPServer(
        name="anamnesis-memory",
        title="Anamnesis shared memory",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )

    @server.tool()
    async def search_memory(
        query: str, limit: int = 8, source: str | None = None, project: str | None = None
    ) -> str:
        """Search past conversations from all AI tools (semantic + keyword).

        Args:
            query: What to look for, in natural language (e.g. "why we chose LanceDB").
            limit: Maximum number of results (1-25).
            source: Only one tool: claude-code, codex, gemini-cli, cursor, chatgpt,
                claude-web, gemini-web, anamnesis or note.
            project: Only conversations from this project directory (absolute path).
        """
        hits = await _service().search(
            query, k=max(1, min(limit, 25)), source=source or None, project=project or None
        )
        return format_hits(hits)

    @server.tool()
    async def recent_conversations(
        limit: int = 15, source: str | None = None, project: str | None = None
    ) -> str:
        """List the most recently active conversations (optionally for one tool or project)."""
        threads = await _service().list_threads(
            limit=max(1, min(limit, 100)), source=source or None, project=project or None
        )
        return format_threads(threads)

    @server.tool()
    async def get_conversation(thread_id: str, last_n: int = 40) -> str:
        """Read a stored conversation (the most recent `last_n` messages) by its thread id."""
        svc = _service()
        thread = await svc.get_thread(thread_id)
        msgs = await svc.messages(thread_id, limit=max(1, min(last_n, 200)))
        head = f"{thread.title} — {_label(thread.source)}" + (
            f" · {thread.project}" if thread.project else ""
        )
        body = "\n\n".join(
            f"[{_when(m.created_at)}] {m.role}: {_snippet(m.content, 4000)}" for m in msgs
        )
        return f"{head}\n\n{body}" if body else f"{head}\n\n(no messages)"

    @server.tool()
    async def remember(note: str, topic: str | None = None) -> str:
        """Save a durable fact, decision or preference to shared memory for future sessions.

        Args:
            note: Self-contained text worth recalling later (include the why).
            topic: Optional grouping, e.g. a project name ("anamnesis") or "preferences".
        """
        thread = await _service().remember(note, topic=topic)
        return f'Saved to "{thread.title}".'

    return server


def main() -> None:
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # stdout carries the MCP protocol: log to a file only.
    logging.basicConfig(
        filename=settings.data_dir / "mcp.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    build_server(settings).run("stdio")
