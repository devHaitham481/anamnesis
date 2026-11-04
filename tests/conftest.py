"""Shared fixtures: an isolated ChatService backed by tmp SQLite/LanceDB and a fake LLM."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import litellm
import pytest

from src.config import Settings
from src.core.chat import ChatService
from src.core.db import Database
from src.core.vector import HashEmbedder, VectorIndex
from src.providers.router import ProviderRouter


class FakeLLM:
    """Stands in for litellm.acompletion; records prompts and replies via LiteLLM's mock path."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply = "Mock reply from the assistant."
        self.fail: Exception | None = None

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return await litellm.acompletion(**kwargs, mock_response=self.reply)

    @property
    def last_messages(self) -> list[dict[str, str]]:
        return list(self.calls[-1]["messages"])


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        anthropic_api_key="fake-ant",  # type: ignore[arg-type]
        gemini_api_key="fake-gem",  # type: ignore[arg-type]
        data_dir=tmp_path / "data",
        rag_min_score=0.3,
        sync_interval=0.05,
    )


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def service(settings: Settings, fake_llm: FakeLLM) -> AsyncIterator[ChatService]:
    db = await Database(settings.db_path).open()
    index = VectorIndex(settings.vector_path, HashEmbedder())
    svc = ChatService(db, ProviderRouter(settings, completion_fn=fake_llm), index, settings)
    yield svc
    await svc.close()
