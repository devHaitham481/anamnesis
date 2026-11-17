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

from anamnesis.core.models import ImportedConversation, ImportedMessage
from anamnesis.ingest.base import text_of, to_epoch
from anamnesis.ingest.clean import clean_text

_UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
# After `thread/revert`, a thread continues in rollout-<ts>-<thread>_<rollout>.jsonl.
_REVERT = re.compile(r"_([0-9a-zA-Z-]+)\.jsonl(?:\.zst)?$")
_INTERNAL_SOURCES = {"guardian_review", "memory_consolidation", "subagent"}


def _internal(payload: dict[str, Any]) -> bool:
    """Sessions Codex runs for itself (reviews, memory, subagents), not conversations."""
    source = payload.get("source")
    if isinstance(source, dict) and ({"subagent", "internal"} & set(source)):
        return True
    return payload.get("thread_source") in _INTERNAL_SOURCES


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
        revert = _REVERT.search(path.name)
        prefix = f"{revert.group(1)}:" if revert else ""  # its ordinals restart: keep them apart
        for n, record in enumerate(_json_lines(_read(path))):
            kind = record.get("type")
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else None
            ts = to_epoch(record.get("timestamp"))
            ext = prefix + str(record.get("ordinal", n))
            if kind == "session_meta" and payload is not None:
                if payload.get("parent_thread_id") or _internal(payload):
                    return  # a subagent's or Codex's own thread, not a conversation
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
                if ptype == "thread_rolled_back":  # the last N turns were undone
                    turns = int(payload.get("num_turns") or 0)
                    items, events = _drop_turns(items, turns), _drop_turns(events, turns)
                    continue
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
            if item.get("phase") == "commentary":
                continue  # progress narration mid-turn, not the answer
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


def _drop_turns(messages: list[ImportedMessage], turns: int) -> list[ImportedMessage]:
    """Remove the last `turns` turns (each starts at a user message)."""
    if turns <= 0:
        return messages
    starts = [i for i, m in enumerate(messages) if m.role == "user"]
    if not starts:
        return messages
    return messages[: starts[-turns]] if turns <= len(starts) else []


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
