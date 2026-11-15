"""Plain-text formatting of search hits and conversations (MCP tool output, `anam search`).

Kept apart from the MCP server so the CLI can use it without importing the MCP SDK.
"""

from __future__ import annotations

import datetime as dt

from anamnesis.core.models import SOURCE_LABELS, SearchHit, Thread

SNIPPET_CHARS = 700


def when(ts: float | None) -> str:
    if not ts:
        return "unknown date"
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def label(source: str | None) -> str:
    return SOURCE_LABELS.get(source or "", source or "unknown")


def snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def format_hits(hits: list[SearchHit]) -> str:
    if not hits:
        return "No matching memories."
    out = []
    for i, h in enumerate(hits, 1):
        where = f" · {h.project}" if h.project else ""
        out.append(
            f"[{i}] {label(h.source)}{where} · {when(h.created_at)} · "
            f'"{h.thread_title or "Untitled"}" (thread {h.thread_id})\n'
            f"{h.role}: {snippet(h.content)}"
        )
    return "\n\n".join(out)


def format_threads(threads: list[Thread]) -> str:
    if not threads:
        return "No conversations stored yet."
    return "\n".join(
        f"- {t.id} · {label(t.source)} · {when(t.updated_at)} · {t.title}"
        + (f" · {t.project}" if t.project else "")
        for t in threads
    )
