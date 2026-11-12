"""Shared fixtures: an isolated MemoryService backed by tmp SQLite/LanceDB (hash embeddings)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from src.config import Settings
from src.core.db import Database
from src.core.memory import MemoryService
from src.core.models import ImportedConversation, ImportedMessage
from src.core.vector import HashEmbedder, VectorIndex


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        semantic_min_score=0.3,
        sync_interval=0.05,
        memory_sync=False,
    )


async def open_service(settings: Settings) -> MemoryService:
    db = await Database(settings.db_path).open()
    return MemoryService(db, VectorIndex(settings.vector_path, HashEmbedder()), settings)


@pytest.fixture
async def service(settings: Settings) -> AsyncIterator[MemoryService]:
    svc = await open_service(settings)
    yield svc
    await svc.close()


def conversation(
    ext: str,
    *texts: str,
    source: str = "claude-code",
    project: str | None = None,
    title: str | None = None,
    start: float = 1_700_000_000,
) -> ImportedConversation:
    """A conversation as an importer would produce it; messages alternate user/assistant."""
    return ImportedConversation(
        source=source,
        external_id=ext,
        title=title,
        project=project,
        messages=[
            ImportedMessage(
                external_id=f"{ext}-{i}",
                role="user" if i % 2 == 0 else "assistant",
                content=t,
                created_at=start + i,
            )
            for i, t in enumerate(texts)
        ],
    )
