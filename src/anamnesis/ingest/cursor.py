"""Cursor: chat ("composer") history in the SQLite file `User/globalStorage/state.vscdb`.

Closed, reverse-engineered format: `cursorDiskKV` rows `composerData:<id>` (session, with
`fullConversationHeadersOnly` ordering) and `bubbleId:<id>:<bubble>` (one message each;
type 1 = user, 2 = assistant). Long conversations keep their ordering in chunk rows
`composerConvHeaders:<id>:<000000>` instead. Older builds kept bubbles inline in
`conversation[]`. Keys are read by range, never with LIKE: these databases reach many GB.
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

from anamnesis.core.models import ImportedConversation, ImportedMessage, Role
from anamnesis.ingest.base import to_epoch
from anamnesis.ingest.clean import clean_text


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
            for key, raw in _prefixed(conn, "composerData:"):
                data = _json(raw)
                if not isinstance(data, dict) or _is_subagent(data):
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
        if not (isinstance(headers, list) and headers):
            headers = []
            for _, raw in _prefixed(conn, f"composerConvHeaders:{composer_id}:"):
                chunk = _json(raw)
                if isinstance(chunk, list):
                    headers.extend(chunk)
        if headers:
            stored = {
                k.rsplit(":", 1)[1]: _json(v)
                for k, v in _prefixed(conn, f"bubbleId:{composer_id}:")
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
            if role is None:
                continue
            # Tool calls and reasoning are skipped, but an answer that also carries reasoning
            # (`thinking`) still has its visible `text`: keep that.
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
        return ImportedConversation(
            source=self.name,
            external_id=composer_id,
            title=data.get("name") or None,
            project=_project(data),
            messages=messages,
        )


def _project(data: dict[str, Any]) -> str | None:
    """The workspace folder, else the first git repo the chat touched."""
    workspace = data.get("workspaceIdentifier")
    if isinstance(workspace, dict):
        uri = workspace.get("uri")
        if isinstance(uri, dict) and (uri.get("fsPath") or uri.get("path")):
            return str(uri.get("fsPath") or uri.get("path"))
    repos = data.get("trackedGitRepos")
    if isinstance(repos, list) and repos and isinstance(repos[0], dict):
        return repos[0].get("repoPath") or None
    return None


def _is_subagent(data: dict[str, Any]) -> bool:
    info = data.get("subagentInfo")
    return bool(data.get("isSubagent") or (isinstance(info, dict) and info.get("parentComposerId")))


def _prefixed(conn: sqlite3.Connection, prefix: str) -> list[tuple[str, Any]]:
    """Rows whose key starts with `prefix`, as an index range scan (LIKE can't use the index)."""
    end = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    return conn.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ? ORDER BY key",
        (prefix, end),
    ).fetchall()


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
