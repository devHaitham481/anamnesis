"""Claude Code: ~/.claude/projects/<project-slug>/<session-id>.jsonl (one JSON record per line)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from anamnesis.core.models import ImportedConversation, ImportedMessage
from anamnesis.ingest.base import text_of, to_epoch
from anamnesis.ingest.clean import clean_text

log = logging.getLogger(__name__)


class ClaudeCodeSource:
    name = "claude-code"

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path.home() / ".claude" / "projects"

    def discover(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        # Top level only: <session>/subagents/*.jsonl are sidechains of the parent session.
        return sorted(self.root.glob("*/*.jsonl"))

    def parse(self, path: Path) -> Iterator[ImportedConversation]:
        messages: list[ImportedMessage] = []
        title: str | None = None
        cwd: str | None = None
        session_id = path.stem
        for record in _records(path):
            kind = record.get("type")
            if kind == "ai-title" and isinstance(record.get("aiTitle"), str):
                title = record["aiTitle"]
            elif kind == "summary" and isinstance(record.get("summary"), str) and not title:
                title = record["summary"]
            if cwd is None and isinstance(record.get("cwd"), str):
                cwd = record["cwd"]
            if kind not in ("user", "assistant"):
                continue
            if record.get("isSidechain") or record.get("isMeta") or record.get("isCompactSummary"):
                continue
            msg = record.get("message")
            if not isinstance(msg, dict):
                continue
            text = clean_text(text_of(msg.get("content"), ("text",)))
            uid = record.get("uuid")
            if not text or not isinstance(uid, str):
                continue
            model = msg.get("model") if kind == "assistant" else None
            messages.append(
                ImportedMessage(
                    external_id=uid,
                    role=kind,
                    content=text,
                    created_at=to_epoch(record.get("timestamp")),
                    model=model if isinstance(model, str) and model != "<synthetic>" else None,
                )
            )
        if messages:
            yield ImportedConversation(
                source=self.name,
                external_id=session_id,
                title=title,
                project=cwd,
                messages=messages,
            )


def _records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # a partially written last line while the session is live
            if isinstance(record, dict):
                yield record
