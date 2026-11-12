"""Shared pieces for importers that read conversation history from other AI tools."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from anamnesis.core.models import ImportedConversation


class Source(Protocol):
    """A tool whose local history Anamnesis can watch (Claude Code, Codex, Gemini CLI, ...)."""

    name: str

    def discover(self) -> list[Path]:
        """Files (or databases) that currently hold this tool's history."""
        ...

    def parse(self, path: Path) -> Iterable[ImportedConversation]:
        """Conversations found in one discovered path. Must be pure (no side effects)."""
        ...


def file_signature(path: Path) -> str:
    """Cheap change detector: a file is re-parsed only when its size or mtime changes."""
    st = path.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def to_epoch(value: Any, default: float = 0.0) -> float:
    """Parse ISO-8601 strings, epoch seconds or epoch milliseconds into epoch seconds."""
    if value is None or value == "":
        return default
    if isinstance(value, int | float):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    if isinstance(value, str):
        s = value.strip()
        try:
            return to_epoch(float(s), default)
        except ValueError:
            pass
        try:
            parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return default
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return parsed.timestamp()
    return default


def text_of(
    content: Any, text_types: tuple[str, ...] = ("text", "input_text", "output_text")
) -> str:
    """Join the plain-text parts of a message `content` (string or list of typed blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type", "text") in text_types:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n\n".join(p for p in parts if p.strip())
