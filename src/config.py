"""Environment-backed configuration. API keys are only ever loaded from the environment / `.env`."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, SecretStr

DEFAULT_CLAUDE_MODEL = "anthropic/claude-sonnet-5-5"
DEFAULT_GEMINI_MODEL = "gemini/gemini-2.5-flash"


class MissingAPIKeyError(RuntimeError):
    """Raised when a provider is used without its API key configured."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    anthropic_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    claude_model: str = DEFAULT_CLAUDE_MODEL
    gemini_model: str = DEFAULT_GEMINI_MODEL

    def require_key(self, provider: str) -> str:
        key = {"anthropic": self.anthropic_api_key, "gemini": self.gemini_api_key}.get(provider)
        if key is None:
            env_name = f"{provider.upper()}_API_KEY"
            raise MissingAPIKeyError(f"{env_name} is not set (add it to .env)")
        return key.get_secret_value()


def _secret(value: str | None) -> SecretStr | None:
    return SecretStr(value) if value and value.strip() else None


def load_settings(
    env_file: str | Path | None = ".env",
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Build Settings from `.env` merged with the process environment (environment wins)."""
    file_values: dict[str, str | None] = {}
    if env_file is not None and Path(env_file).is_file():
        file_values = dict(dotenv_values(env_file))
    env = {**file_values, **(os.environ if environ is None else environ)}

    return Settings(
        anthropic_api_key=_secret(env.get("ANTHROPIC_API_KEY")),
        gemini_api_key=_secret(env.get("GEMINI_API_KEY")),
        claude_model=env.get("NEXUS_CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL,
        gemini_model=env.get("NEXUS_GEMINI_MODEL") or DEFAULT_GEMINI_MODEL,
    )
