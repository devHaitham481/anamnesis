"""Shared fixtures: an isolated ChatService backed by tmp SQLite/LanceDB and a fake LLM."""

from __future__ import annotations

import asyncio
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
        memory_sync=False,
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


class _Delta:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str | None, finish: str | None) -> None:
        self.delta = _Delta(content)
        self.finish_reason = finish


class _Chunk:
    def __init__(self, content: str | None, finish: str | None = None) -> None:
        self.choices = [_Choice(content, finish)]


class SlowStream:
    """Async-iterable provider stream that waits on a gate between chunks; tracks aclose()."""

    def __init__(self, words: list[str], gate: asyncio.Event | None, delay: float) -> None:
        self.words = words
        self.gate = gate
        self.delay = delay
        self.closed = False

    def __aiter__(self) -> SlowStream:
        self._i = 0
        return self

    async def __anext__(self) -> _Chunk:
        if self._i >= len(self.words):
            raise StopAsyncIteration
        if self._i > 0:
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(self.delay)
        self._i += 1
        last = self._i == len(self.words)
        return _Chunk(self.words[self._i - 1], "stop" if last else None)

    async def aclose(self) -> None:
        self.closed = True


class SlowLLM:
    """Fake provider that streams word by word; set `gate` to hold the stream after word 1."""

    def __init__(self, words: int = 20, delay: float = 0.0) -> None:
        self.words = [f"w{i} " for i in range(words)]
        self.delay = delay
        self.gate: asyncio.Event | None = None
        self.streams: list[SlowStream] = []

    async def __call__(self, **kwargs: Any) -> SlowStream:
        stream = SlowStream(self.words, self.gate, self.delay)
        self.streams.append(stream)
        return stream
