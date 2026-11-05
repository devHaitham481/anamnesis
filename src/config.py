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
DEFAULT_DATA_DIR = Path.home() / ".nexus-ai"


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
    auth_token: SecretStr | None = None
    allowed_hosts: tuple[str, ...] = ()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nexus.db"

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
    if env_file is not None and Path(env_file).is_file():
        file_values = dict(dotenv_values(env_file))
    env = {**file_values, **(os.environ if environ is None else environ)}

    def get(name: str) -> str | None:
        value = env.get(name)
        return value.strip() if value and value.strip() else None

    defaults = Settings()
    return Settings(
        anthropic_api_key=_secret(env.get("ANTHROPIC_API_KEY")),
        gemini_api_key=_secret(env.get("GEMINI_API_KEY")),
        claude_model=get("NEXUS_CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL,
        gemini_model=get("NEXUS_GEMINI_MODEL") or DEFAULT_GEMINI_MODEL,
        default_model=get("NEXUS_DEFAULT_MODEL") or defaults.default_model,
        request_timeout=float(get("NEXUS_REQUEST_TIMEOUT") or defaults.request_timeout),
        max_retries=int(get("NEXUS_MAX_RETRIES") or defaults.max_retries),
        data_dir=Path(get("NEXUS_DATA_DIR") or DEFAULT_DATA_DIR).expanduser(),
        embedding_model=get("NEXUS_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL,
        rag_enabled=_bool(env.get("NEXUS_RAG"), defaults.rag_enabled),
        rag_top_k=int(get("NEXUS_RAG_TOP_K") or defaults.rag_top_k),
        rag_min_score=float(get("NEXUS_RAG_MIN_SCORE") or defaults.rag_min_score),
        history_limit=int(get("NEXUS_HISTORY_LIMIT") or defaults.history_limit),
        auth_token=_secret(env.get("NEXUS_AUTH_TOKEN")),
        allowed_hosts=tuple(
            h.strip() for h in (get("NEXUS_ALLOWED_HOSTS") or "").split(",") if h.strip()
        ),
    )
