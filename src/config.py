"""Environment-backed configuration, read from the process environment and `.env`."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, SecretStr

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DATA_DIR = Path.home() / ".anamnesis"
LEGACY_DATA_DIR = Path.home() / ".nexus-ai"  # before the rename to Anamnesis
PROJECT_ENV = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_dir: Path = DEFAULT_DATA_DIR
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    semantic_search: bool = True  # off = keyword search only (no embedding model download)
    semantic_min_score: float = 0.6
    sync_interval: float = 1.0  # how often the TUI / web UI check the database for changes
    memory_sync: bool = True  # import other AI tools' history in the background
    memory_sync_interval: float = 60.0
    auth_token: SecretStr | None = None
    allowed_hosts: tuple[str, ...] = ()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "anamnesis.db"

    @property
    def vector_path(self) -> Path:
        return self.data_dir / "vectors"


def _secret(value: str | None) -> SecretStr | None:
    return SecretStr(value) if value and value.strip() else None


def _bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def load_settings(
    env_file: str | Path | None = ".env",
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Build Settings from `.env` merged with the process environment (environment wins)."""
    file_values: dict[str, str | None] = {}
    path = Path(env_file) if env_file is not None else None
    if path is not None and not path.is_file() and str(env_file) == ".env":
        # Launched from elsewhere (e.g. by Claude Desktop as an MCP server): use the project's .env.
        path = PROJECT_ENV
    if path is not None and path.is_file():
        file_values = dict(dotenv_values(path))
    env = {**file_values, **(os.environ if environ is None else environ)}

    def get(*names: str) -> str | None:
        # ANAMNESIS_* wins; the pre-rename NEXUS_* names still work. Extra names are fallbacks.
        for name in names:
            for key in (name, name.replace("ANAMNESIS_", "NEXUS_", 1)):
                value = env.get(key)
                if value and value.strip():
                    return value.strip()
        return None

    defaults = Settings()
    data_dir = Path(get("ANAMNESIS_DATA_DIR") or DEFAULT_DATA_DIR).expanduser()
    if environ is None and get("ANAMNESIS_DATA_DIR") is None:
        migrate_legacy_data_dir(LEGACY_DATA_DIR, data_dir)
    return Settings(
        data_dir=data_dir,
        embedding_model=get("ANAMNESIS_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL,
        # ANAMNESIS_RAG* are the names from when Anamnesis also had a chat gateway.
        semantic_search=_bool(
            get("ANAMNESIS_SEMANTIC_SEARCH", "ANAMNESIS_RAG"), defaults.semantic_search
        ),
        semantic_min_score=float(
            get("ANAMNESIS_SEMANTIC_MIN_SCORE", "ANAMNESIS_RAG_MIN_SCORE")
            or defaults.semantic_min_score
        ),
        memory_sync=_bool(get("ANAMNESIS_MEMORY_SYNC"), defaults.memory_sync),
        memory_sync_interval=float(
            get("ANAMNESIS_MEMORY_SYNC_INTERVAL") or defaults.memory_sync_interval
        ),
        auth_token=_secret(get("ANAMNESIS_AUTH_TOKEN")),
        allowed_hosts=tuple(
            h.strip() for h in (get("ANAMNESIS_ALLOWED_HOSTS") or "").split(",") if h.strip()
        ),
    )


def migrate_legacy_data_dir(legacy: Path, target: Path) -> bool:
    """Move a pre-rename `~/.nexus-ai` (and its `nexus.db`) to `~/.anamnesis`, once."""
    if target.exists() or not legacy.is_dir():
        return False
    legacy.rename(target)
    for old, new in (("nexus.db", "anamnesis.db"), ("nexus.log", "anamnesis.log")):
        for suffix in ("", "-wal", "-shm"):
            src = target / f"{old}{suffix}"
            if src.exists():
                src.rename(target / f"{new}{suffix}")
    return True
