"""`anam setup`: the guided first run, against a fake home (never touches real tool configs)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from anamnesis.cli.__main__ import main


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("ANAMNESIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ANAMNESIS_SEMANTIC_SEARCH", "false")
    monkeypatch.setattr("anamnesis.ingest.runner.default_sources", lambda: [])
    monkeypatch.setattr("anamnesis.connect._claude_bin", lambda: None)  # no real `claude`
    monkeypatch.setattr("shutil.which", lambda name: None)
    return home


def gemini_entry(home: Path) -> dict[str, object] | None:
    path = home / ".gemini" / "settings.json"
    if not path.is_file():
        return None
    servers: dict[str, dict[str, object]] = json.loads(path.read_text()).get("mcpServers", {})
    return servers.get("anamnesis")


def test_setup_with_no_tools(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["setup", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "None of the supported tools were found" in out
    assert "Nothing to connect" in out and "Done." in out


def test_setup_connects_found_tools_and_is_rerunnable(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (home / ".gemini").mkdir()
    assert main(["setup", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "✓ Gemini CLI" in out and "Restart Gemini CLI" in out
    entry = gemini_entry(home)
    assert entry is not None and entry["args"] == ["-m", "anamnesis.memory_mcp"]

    assert main(["setup", "--yes", "--skip-import"]) == 0  # second run: nothing to change
    assert "already connected" in capsys.readouterr().out


def test_setup_asks_before_connecting(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (home / ".gemini").mkdir()
    # Piped (curl | sh) with no terminal: never edit configs without asking.
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["setup", "--skip-import"]) == 0
    assert "Run `anam connect all` when ready" in capsys.readouterr().out
    assert gemini_entry(home) is None

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", Tty("n\n"))
    assert main(["setup", "--skip-import"]) == 0
    assert gemini_entry(home) is None
    monkeypatch.setattr("sys.stdin", Tty("\n"))  # Enter = yes
    assert main(["setup", "--skip-import"]) == 0
    assert gemini_entry(home) is not None


def test_disconnect_all(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (home / ".gemini").mkdir()
    main(["connect", "all"])
    assert gemini_entry(home) is not None
    assert main(["disconnect", "all"]) == 0
    assert gemini_entry(home) is None
    assert main(["disconnect", "all"]) == 0
    assert "No AI tool is connected" in capsys.readouterr().out
