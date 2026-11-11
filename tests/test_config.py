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
        env, environ={"ANTHROPIC_API_KEY": "from-env", "ANAMNESIS_GEMINI_MODEL": "gemini/x"}
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


def test_legacy_nexus_env_names_still_work() -> None:
    from src.config import load_settings

    s = load_settings(
        env_file=None, environ={"NEXUS_RAG_TOP_K": "9", "ANAMNESIS_HISTORY_LIMIT": "7"}
    )
    assert (s.rag_top_k, s.history_limit) == (9, 7)
    s = load_settings(env_file=None, environ={"NEXUS_RAG_TOP_K": "9", "ANAMNESIS_RAG_TOP_K": "3"})
    assert s.rag_top_k == 3


def test_migrate_legacy_data_dir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from src.config import migrate_legacy_data_dir

    old, new = tmp_path / ".nexus-ai", tmp_path / ".anamnesis"
    (old / "vectors").mkdir(parents=True)
    for name in ("nexus.db", "nexus.db-wal", "nexus.log"):
        (old / name).write_text(name)
    assert migrate_legacy_data_dir(old, new) is True
    assert not old.exists()
    assert sorted(p.name for p in new.iterdir()) == [
        "anamnesis.db",
        "anamnesis.db-wal",
        "anamnesis.log",
        "vectors",
    ]
    assert migrate_legacy_data_dir(old, new) is False  # idempotent
