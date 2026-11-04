from pathlib import Path

import pytest

from src.config import DEFAULT_CLAUDE_MODEL, MissingAPIKeyError, load_settings


def test_loads_keys_from_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant\nGEMINI_API_KEY=gem\n")
    s = load_settings(env, environ={})
    assert s.require_key("anthropic") == "sk-ant"
    assert s.require_key("gemini") == "gem"
    assert s.claude_model == DEFAULT_CLAUDE_MODEL


def test_process_env_overrides_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=from-file\n")
    s = load_settings(
        env, environ={"ANTHROPIC_API_KEY": "from-env", "NEXUS_GEMINI_MODEL": "gemini/x"}
    )
    assert s.require_key("anthropic") == "from-env"
    assert s.gemini_model == "gemini/x"


def test_missing_and_blank_keys_raise(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "nope.env", environ={"GEMINI_API_KEY": "  "})
    with pytest.raises(MissingAPIKeyError, match="ANTHROPIC_API_KEY"):
        s.require_key("anthropic")
    with pytest.raises(MissingAPIKeyError, match="GEMINI_API_KEY"):
        s.require_key("gemini")


def test_secrets_not_leaked_in_repr() -> None:
    s = load_settings(None, environ={"ANTHROPIC_API_KEY": "super-secret"})
    assert "super-secret" not in repr(s)
