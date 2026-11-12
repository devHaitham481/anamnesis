from pathlib import Path

from src.config import DEFAULT_EMBEDDING_MODEL, Settings, load_settings


def test_defaults_need_no_api_keys(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "nope.env", environ={})
    assert s.semantic_search and s.memory_sync
    assert s.embedding_model == DEFAULT_EMBEDDING_MODEL
    assert not any("key" in name for name in Settings.model_fields)  # no provider keys at all


def test_process_env_overrides_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ANAMNESIS_MEMORY_SYNC_INTERVAL=30\nANAMNESIS_SEMANTIC_SEARCH=false\n")
    s = load_settings(env, environ={"ANAMNESIS_MEMORY_SYNC_INTERVAL": "5"})
    assert s.memory_sync_interval == 5.0
    assert s.semantic_search is False


def test_old_keys_in_env_file_are_ignored(tmp_path: Path) -> None:
    """A .env from the chat-gateway days must still load."""
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-old\nANAMNESIS_DEFAULT_MODEL=gemini\n")
    s = load_settings(env, environ={})
    assert "sk-old" not in repr(s)


def test_secrets_not_leaked_in_repr() -> None:
    s = load_settings(None, environ={"ANAMNESIS_AUTH_TOKEN": "super-secret"})
    assert s.auth_token is not None and "super-secret" not in repr(s)


def test_legacy_env_names_still_work() -> None:
    s = load_settings(env_file=None, environ={"NEXUS_RAG": "off", "NEXUS_RAG_MIN_SCORE": "0.4"})
    assert (s.semantic_search, s.semantic_min_score) == (False, 0.4)
    s = load_settings(
        env_file=None, environ={"ANAMNESIS_RAG": "off", "ANAMNESIS_SEMANTIC_SEARCH": "on"}
    )
    assert s.semantic_search is True  # the new name wins


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
