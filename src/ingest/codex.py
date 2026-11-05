"""OpenAI Codex CLI: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl[.zst] (one RolloutLine per line).

Format per openai/codex `codex-rs/rollout`: {"timestamp", "type", "payload"}; older builds wrote
bare ResponseItems without the wrapper, which are handled too.
"""

from __future__ import annotations

import io
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from src.core.models import ImportedConversation, ImportedMessage
from src.ingest.base import text_of, to_epoch
from src.ingest.clean import clean_text

_UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


class CodexSource:
    name = "codex"

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or codex_home()

    def discover(self) -> list[Path]:
        found: list[Path] = []
        for sub in ("sessions", "archived_sessions"):
            base = self.root / sub
            if base.is_dir():
                found += base.rglob("rollout-*.jsonl")
                found += base.rglob("rollout-*.jsonl.zst")
        return sorted(found)

    def _titles(self) -> dict[str, str]:
        titles: dict[str, str] = {}
        index = self.root / "session_index.jsonl"
        if index.is_file():
            for row in _json_lines(index.read_text(encoding="utf-8", errors="replace")):
                if isinstance(row.get("id"), str) and isinstance(row.get("thread_name"), str):
                    titles[row["id"]] = row["thread_name"]  # last row wins
        return titles

    def parse(self, path: Path) -> Iterator[ImportedConversation]:
        session_id: str | None = None
        cwd: str | None = None
        model: str | None = None
        items: list[ImportedMessage] = []
        events: list[ImportedMessage] = []
        for n, record in enumerate(_json_lines(_read(path))):
            kind = record.get("type")
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else None
            ts = to_epoch(record.get("timestamp"))
            ext = str(record.get("ordinal", n))
            if kind == "session_meta" and payload is not None:
                if payload.get("parent_thread_id"):
                    return  # a subagent's thread; its parent session holds the conversation
                session_id = payload.get("id") or session_id
                cwd = payload.get("cwd") or cwd
                continue
            if "instructions" in record and "id" in record and payload is None:  # legacy header
                session_id = session_id or record.get("id")
                continue
            if kind == "turn_context" and payload is not None:
                model = payload.get("model") or model
                continue
            if kind == "event_msg" and payload is not None:
                ptype = payload.get("type")
                role = {"user_message": "user", "agent_message": "assistant"}.get(ptype or "")
                text = clean_text(payload.get("message") or "") if role else ""
                if role and text:
                    events.append(_msg(ext, role, text, ts, model))
                continue
            item = payload if kind == "response_item" else (record if kind == "message" else None)
            if item is None or item.get("type") != "message":
                continue
            role = item.get("role")
            if role not in ("user", "assistant"):
                continue  # developer/system prompts
            text = clean_text(text_of(item.get("content")))
            if text:
                items.append(_msg(ext, role, text, ts or to_epoch(item.get("timestamp")), model))
        # ResponseItems are the canonical history; UI events are the fallback.
        messages = items or events
        if not messages:
            return
        if session_id is None:
            match = _UUID.search(path.name)
            session_id = match.group(1) if match else path.stem
        yield ImportedConversation(
            source=self.name,
            external_id=session_id,
            title=self._titles().get(session_id),
            project=cwd,
            messages=messages,
        )


def _msg(ext: str, role: str, text: str, ts: float, model: str | None) -> ImportedMessage:
    return ImportedMessage(
        external_id=ext,
        role="user" if role == "user" else "assistant",
        content=text,
        created_at=ts,
        model=model if role == "assistant" else None,
    )


def _read(path: Path) -> str:
    if path.suffix == ".zst":
        import zstandard

        with path.open("rb") as fh, zstandard.ZstdDecompressor().stream_reader(fh) as reader:
            return io.TextIOWrapper(reader, encoding="utf-8", errors="replace").read()
    return path.read_text(encoding="utf-8", errors="replace")


def _json_lines(text: str) -> Iterator[dict[str, Any]]:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj
