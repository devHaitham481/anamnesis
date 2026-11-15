"""The archive: what is kept, Claude Code's own deletion, and counting agent lookups."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from anamnesis.cli.__main__ import main
from anamnesis.core.memory import MemoryService
from anamnesis.core.models import ImportedConversation, ImportedMessage
from anamnesis.retention import (
    KEEP_DAYS,
    claude_code_cleanup_days,
    deletes_history,
    keep_claude_code_history,
    kept_after_deletion,
)


def conv(ext: str, source: str, when: float, *texts: str) -> ImportedConversation:
    return ImportedConversation(
        source=source,
        external_id=ext,
        title=None,
        project=None,
        messages=[
            ImportedMessage(
                external_id=f"{ext}-{i}",
                role="user" if i % 2 == 0 else "assistant",
                content=t,
                created_at=when + i,
            )
            for i, t in enumerate(texts)
        ],
    )


@pytest.fixture
def claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


def test_claude_code_retention(claude_home: Path) -> None:
    assert claude_code_cleanup_days() is None  # Claude Code not used here
    (claude_home / "projects").mkdir(parents=True)
    assert claude_code_cleanup_days() == 30  # the default, no settings file
    settings = claude_home / "settings.json"
    settings.write_text(json.dumps({"model": "opus", "cleanupPeriodDays": 90}))
    assert claude_code_cleanup_days() == 90 and deletes_history(90)

    assert "set cleanupPeriodDays" in keep_claude_code_history()
    data = json.loads(settings.read_text())
    assert data == {"model": "opus", "cleanupPeriodDays": KEEP_DAYS}  # merged, not replaced
    assert json.loads((claude_home / "settings.json.bak").read_text())["cleanupPeriodDays"] == 90
    assert not deletes_history(claude_code_cleanup_days())
    assert keep_claude_code_history() == "already keeping its history"


def test_keep_refuses_a_broken_settings_file(claude_home: Path) -> None:
    (claude_home / "projects").mkdir(parents=True)
    (claude_home / "settings.json").write_text("[1, 2]")
    with pytest.raises(ValueError):
        keep_claude_code_history()
    assert (claude_home / "settings.json").read_text() == "[1, 2]"


def test_kept_after_deletion(tmp_path: Path) -> None:
    alive = tmp_path / "alive.jsonl"
    alive.write_text("{}")
    keys = [
        f"claude-code:{alive}",
        f"claude-code:{tmp_path / 'gone.jsonl'}",
        f"codex:{tmp_path / 'gone2.jsonl'}",
        f"export:{tmp_path / 'chatgpt.zip'}",  # exports aren't the tool's own copy
    ]
    assert kept_after_deletion(keys) == {"Claude Code": 1, "Codex": 1}


async def test_archive_summary_skips_empty_and_counts_the_week(service: MemoryService) -> None:
    now = time.time()
    await service.import_conversation(conv("a", "claude-code", now - 3600, "recent work", "ok"))
    await service.import_conversation(conv("b", "claude-code", now - 40 * 86400, "old", "ok"))
    await service.import_conversation(conv("c", "chatgpt", now - 90 * 86400, "chat", "ok"))
    await service.db.create_thread("New chat")  # empty leftover: not part of the archive
    summary = await service.db.archive_summary(now - 7 * 86400)
    assert summary["sources"] == {"claude-code": (2, 1), "chatgpt": (1, 0)}
    assert summary["messages"] == 6
    assert summary["first"] == pytest.approx(now - 90 * 86400)


def test_status_and_keep_commands(
    tmp_path: Path, claude_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ANAMNESIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ANAMNESIS_SEMANTIC_SEARCH", "false")
    monkeypatch.setattr("anamnesis.connect._claude_bin", lambda: None)
    monkeypatch.setattr("shutil.which", lambda name: None)
    (claude_home / "projects").mkdir(parents=True)

    main(["status"])
    out = capsys.readouterr().out
    assert "nothing archived yet" in out
    assert "claude.ai" in out and "anam import" in out  # how to add web chats

    assert main(["keep"]) == 0
    assert "cleanupPeriodDays" in capsys.readouterr().out
    assert claude_code_cleanup_days() == KEEP_DAYS


def test_search_shorthand() -> None:
    from anamnesis.cli.__main__ import _search_shorthand as sh

    assert sh(["preview", "environments"]) == ["search", "preview", "environments"]
    assert sh(["preview environments"]) == ["search", "preview", "environments"]
    assert sh(["search preview environments"]) == ["search", "preview environments"]
    assert sh(["search", "preview", "environments"]) == ["search", "preview", "environments"]
    assert sh(["import", "My Export.zip"]) == ["import", "My Export.zip"]
    assert sh(["status"]) == ["status"] and sh([]) == [] and sh(["--version"]) == ["--version"]
