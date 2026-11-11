"""Environment-backed configuration. API keys are only ever loaded from the environment / `.env`."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, SecretStr

DEFAULT_CLAUDE_MODEL = "anthropic/claude-sonnet-5-5"
DEFAULT_GEMINI_MODEL = "gemini/gemini-3.8-flash"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DATA_DIR = Path.home() / ".anamnesis"
LEGACY_DATA_DIR = Path.home() / ".nexus-ai"  # before the rename to Anamnesis
PROJECT_ENV = Path(__file__).resolve().parents[1] / ".env"


class MissingAPIKeyError(RuntimeError):
    """Raised when a provider is used without its API key configured."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    anthropic_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    claude_model: str = DEFAULT_CLAUDE_MODEL
    gemini_model: str = DEFAULT_GEMINI_MODEL
    default_model: str = "claude"
    request_timeout: float = 120.0
    max_retries: int = 2

    data_dir: Path = DEFAULT_DATA_DIR
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    rag_enabled: bool = True
    rag_top_k: int = 4
    rag_min_score: float = 0.6
    history_limit: int = 40
    sync_interval: float = 1.0
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

    def require_key(self, provider: str) -> str:
        key = {"anthropic": self.anthropic_api_key, "gemini": self.gemini_api_key}.get(provider)
        if key is None:
            env_name = f"{provider.upper()}_API_KEY"
            raise MissingAPIKeyError(f"{env_name} is not set (add it to .env)")
        return key.get_secret_value()


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

    def get(name: str) -> str | None:
        # ANAMNESIS_* wins; the pre-rename NEXUS_* names still work.
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
        anthropic_api_key=_secret(env.get("ANTHROPIC_API_KEY")),
        gemini_api_key=_secret(env.get("GEMINI_API_KEY")),
        claude_model=get("ANAMNESIS_CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL,
        gemini_model=get("ANAMNESIS_GEMINI_MODEL") or DEFAULT_GEMINI_MODEL,
        default_model=get("ANAMNESIS_DEFAULT_MODEL") or defaults.default_model,
        request_timeout=float(get("ANAMNESIS_REQUEST_TIMEOUT") or defaults.request_timeout),
        max_retries=int(get("ANAMNESIS_MAX_RETRIES") or defaults.max_retries),
        data_dir=data_dir,
        embedding_model=get("ANAMNESIS_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL,
        rag_enabled=_bool(get("ANAMNESIS_RAG"), defaults.rag_enabled),
        rag_top_k=int(get("ANAMNESIS_RAG_TOP_K") or defaults.rag_top_k),
        rag_min_score=float(get("ANAMNESIS_RAG_MIN_SCORE") or defaults.rag_min_score),
        history_limit=int(get("ANAMNESIS_HISTORY_LIMIT") or defaults.history_limit),
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
