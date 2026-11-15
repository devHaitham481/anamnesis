"""How long AI tools keep their own history, and what Anamnesis has kept beyond that.

Claude Code deletes session transcripts that have been inactive for `cleanupPeriodDays`
(30 by default). Anamnesis keeps its own copy either way; turning the deletion off as well
keeps old sessions resumable in Claude Code itself.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

CLAUDE_CODE_DEFAULT_DAYS = 30
KEEP_DAYS = 36500  # "forever": a hundred years

# Sources whose imported files are the tool's own copy (one file per session).
_SESSION_FILE_SOURCES = {"claude-code": "Claude Code", "codex": "Codex", "gemini-cli": "Gemini CLI"}


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _settings_path() -> Path:
    return claude_dir() / "settings.json"


def claude_code_cleanup_days() -> int | None:
    """Days after which Claude Code deletes idle sessions; None if Claude Code isn't used here."""
    if not (claude_dir() / "projects").is_dir():
        return None
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8") or "{}")
    except FileNotFoundError:
        return CLAUDE_CODE_DEFAULT_DAYS
    except (ValueError, OSError):
        return None  # unreadable: don't guess
    days = data.get("cleanupPeriodDays") if isinstance(data, dict) else None
    return days if isinstance(days, int) and days > 0 else CLAUDE_CODE_DEFAULT_DAYS


def deletes_history(days: int | None) -> bool:
    return days is not None and days < KEEP_DAYS


def keep_claude_code_history() -> str:
    """Set `cleanupPeriodDays` in Claude Code's user settings (merged; a .bak copy is kept)."""
    path = _settings_path()
    data: dict[str, Any] = {}
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} is not a JSON object; not touching it")
        data = loaded
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    if data.get("cleanupPeriodDays") == KEEP_DAYS:
        return "already keeping its history"
    data["cleanupPeriodDays"] = KEEP_DAYS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"set cleanupPeriodDays = {KEEP_DAYS} in {path}"


def kept_after_deletion(ingested: list[str]) -> dict[str, int]:
    """Sessions Anamnesis still has whose original file the tool has since deleted, per tool."""
    kept: dict[str, int] = {}
    for key in ingested:
        source, sep, path = key.partition(":")
        if sep and source in _SESSION_FILE_SOURCES and not Path(path).exists():
            label = _SESSION_FILE_SOURCES[source]
            kept[label] = kept.get(label, 0) + 1
    return kept
