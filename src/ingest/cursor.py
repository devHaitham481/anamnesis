"""Cursor: chat ("composer") history in the SQLite file `User/globalStorage/state.vscdb`.

Closed, reverse-engineered format: `cursorDiskKV` rows `composerData:<id>` (session, with
`fullConversationHeadersOnly` ordering) and `bubbleId:<id>:<bubble>` (one message each;
type 1 = user, 2 = assistant). Older builds kept bubbles inline in `conversation[]`.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from src.core.models import ImportedConversation, ImportedMessage, Role
from src.ingest.base import to_epoch
from src.ingest.clean import clean_text


def cursor_user_dir() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Cursor" / "User"
    if sys.platform.startswith("win"):
        return home / "AppData" / "Roaming" / "Cursor" / "User"
    return home / ".config" / "Cursor" / "User"


class CursorSource:
    name = "cursor"

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or cursor_user_dir() / "globalStorage" / "state.vscdb"

    def discover(self) -> list[Path]:
        return [self.db_path] if self.db_path.is_file() else []

    def signature(self, path: Path) -> str:
        # Cursor writes through SQLite's WAL, so the main file's mtime alone misses new chats.
        parts = []
        for p in (path, path.with_name(path.name + "-wal")):
            if p.exists():
                st = p.stat()
                parts.append(f"{st.st_size}:{st.st_mtime_ns}")
        return "|".join(parts)

    def parse(self, path: Path) -> Iterator[ImportedConversation]:
        with _open_readonly(path) as conn:
            if not _has_table(conn, "cursorDiskKV"):
                return
            rows = conn.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            ).fetchall()
            for key, raw in rows:
                data = _json(raw)
                if not isinstance(data, dict) or data.get("isSubagent"):
                    continue
                composer_id = str(data.get("composerId") or key.split(":", 1)[1])
                conv = self._conversation(conn, composer_id, data)
                if conv is not None:
                    yield conv

    def _conversation(
        self, conn: sqlite3.Connection, composer_id: str, data: dict[str, Any]
    ) -> ImportedConversation | None:
        created = to_epoch(data.get("createdAt"))
        bubbles: list[dict[str, Any]] = []
        headers = data.get("fullConversationHeadersOnly")
        if isinstance(headers, list) and headers:
            stored = {
                k.rsplit(":", 1)[1]: _json(v)
                for k, v in conn.execute(
                    "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
                    (f"bubbleId:{composer_id}:%",),
                )
            }
            for h in headers:
                bubble = stored.get(str(h.get("bubbleId"))) if isinstance(h, dict) else None
                if isinstance(bubble, dict):
                    bubbles.append(bubble)
        elif isinstance(data.get("conversation"), list):
            bubbles = [b for b in data["conversation"] if isinstance(b, dict)]
        messages = []
        for i, b in enumerate(bubbles):
            role = {1: "user", 2: "assistant", "user": "user", "ai": "assistant"}.get(b.get("type"))
            if role is None or b.get("toolFormerData") or b.get("thinking"):
                continue
            text = clean_text(str(b.get("text") or b.get("rawText") or ""))
            if not text:
                continue
            model_info = b.get("modelInfo")
            messages.append(
                ImportedMessage(
                    external_id=str(b.get("bubbleId") or i),
                    role=cast(Role, role),
                    content=text,
                    created_at=to_epoch(b.get("createdAt"), created),
                    model=model_info.get("modelName") if isinstance(model_info, dict) else None,
                )
            )
        if not messages:
            return None
        workspace = data.get("workspaceIdentifier")
        project = None
        if isinstance(workspace, dict):
            uri = workspace.get("uri")
            if isinstance(uri, dict):
                project = uri.get("fsPath") or uri.get("path")
        return ImportedConversation(
            source=self.name,
            external_id=composer_id,
            title=data.get("name") or None,
            project=project,
            messages=messages,
        )


class _open_readonly:  # noqa: N801 - used as a context manager
    """Open Cursor's live database read-only; fall back to a private copy if it is locked."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.tmp: tempfile.TemporaryDirectory[str] | None = None
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        try:
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=2)
            self.conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        except sqlite3.Error:
            self.tmp = tempfile.TemporaryDirectory()
            copy = Path(self.tmp.name) / self.path.name
            for suffix in ("", "-wal", "-shm"):
                src = self.path.with_name(self.path.name + suffix)
                if src.exists():
                    shutil.copy2(src, copy.with_name(copy.name + suffix))
            self.conn = sqlite3.connect(copy)
        return self.conn

    def __exit__(self, *exc: object) -> None:
        if self.conn is not None:
            self.conn.close()
        if self.tmp is not None:
            self.tmp.cleanup()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _json(raw: Any) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
