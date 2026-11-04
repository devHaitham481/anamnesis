"""ChatService: the single orchestration point used by both the TUI and the web server.

Flow for one turn: persist user message -> retrieve related snippets -> assemble prompt ->
stream from the provider router -> persist assistant message -> index both in the vector store.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

from src.config import MissingAPIKeyError, Settings
from src.core.context import assemble_prompt
from src.core.db import Database
from src.core.models import Message, SearchHit, Thread
from src.core.vector import FastEmbedEmbedder, VectorIndex
from src.providers.router import ProviderRouter

log = logging.getLogger(__name__)

TITLE_CHARS = 60
INTERRUPTED_NOTE = "\n\n_[response interrupted]_"


@dataclass(frozen=True)
class UserSaved:
    message: Message
    type: Literal["user_saved"] = "user_saved"


@dataclass(frozen=True)
class Delta:
    thread_id: str
    text: str
    type: Literal["delta"] = "delta"


@dataclass(frozen=True)
class AssistantSaved:
    message: Message
    type: Literal["assistant_saved"] = "assistant_saved"


ChatEvent = UserSaved | Delta | AssistantSaved


def make_title(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= TITLE_CHARS else line[: TITLE_CHARS - 1].rstrip() + "…"


class ChatService:
    def __init__(
        self,
        db: Database,
        router: ProviderRouter,
        index: VectorIndex | None,
        settings: Settings,
    ) -> None:
        self.db = db
        self.router = router
        self.index = index
        self.settings = settings

    # ---------- lifecycle ----------

    @classmethod
    async def create(cls, settings: Settings, router: ProviderRouter | None = None) -> ChatService:
        """Open the database and (best-effort) the vector index from settings."""
        db = await Database(settings.db_path).open()
        router = router or ProviderRouter(settings)
        for alias, model in router.aliases.items():
            await db.upsert_provider(alias, model)
        index: VectorIndex | None = None
        if settings.rag_enabled:
            try:
                embedder = await asyncio.to_thread(
                    FastEmbedEmbedder, settings.embedding_model, settings.data_dir / "models"
                )
                index = await asyncio.to_thread(VectorIndex, settings.vector_path, embedder)
            except Exception as e:  # offline first run, corrupted cache, ...
                log.warning("Semantic memory disabled (embedder unavailable): %s", e)
        service = cls(db, router, index, settings)
        await service.backfill_index()
        return service

    async def close(self) -> None:
        await self.db.close()

    @property
    def models(self) -> list[str]:
        return list(self.router.aliases)

    @property
    def default_model(self) -> str:
        d = self.settings.default_model
        return d if d in self.router.aliases else self.models[0]

    async def backfill_index(self) -> int:
        """Index any messages written while the vector store was unavailable."""
        if self.index is None:
            return 0
        index = self.index
        indexed = await asyncio.to_thread(index.indexed_ids)
        missing = [m for m in await self.db.all_messages() if m.id not in indexed]
        if missing:
            await asyncio.to_thread(index.add, missing)
        return len(missing)

    # ---------- threads ----------

    async def new_thread(self, model: str | None = None, title: str = "New chat") -> Thread:
        model = model or self.default_model
        self.router.resolve(model)  # validate early
        return await self.db.create_thread(title=title, model=model)

    async def list_threads(self) -> list[Thread]:
        return await self.db.list_threads()

    async def get_thread(self, thread_id: str) -> Thread:
        return await self.db.get_thread(thread_id)

    async def rename_thread(self, thread_id: str, title: str) -> Thread:
        return await self.db.update_thread(thread_id, title=make_title(title) or "Untitled")

    async def set_thread_model(self, thread_id: str, model: str) -> Thread:
        self.router.resolve(model)
        return await self.db.update_thread(thread_id, model=model)

    async def delete_thread(self, thread_id: str) -> None:
        await self.db.delete_thread(thread_id)
        if self.index is not None:
            try:
                await asyncio.to_thread(self.index.delete_thread, thread_id)
            except Exception as e:
                log.warning("Failed to remove thread %s from vector index: %s", thread_id, e)

    async def messages(self, thread_id: str, limit: int | None = None) -> list[Message]:
        return await self.db.get_messages(thread_id, limit=limit)

    # ---------- change feed (cross-process sync between TUI and web) ----------

    async def latest_message_id(self) -> int:
        return await self.db.max_message_id()

    async def messages_after(self, after_id: int) -> list[Message]:
        return await self.db.messages_after(after_id)

    async def threads_signature(self) -> tuple[int, float, int]:
        return await self.db.threads_signature()

    def missing_key(self, model: str) -> str | None:
        """Name of the env var that must be set to use `model`, or None if it is usable."""
        provider, _ = self.router.resolve(model)
        try:
            self.settings.require_key(provider)
        except MissingAPIKeyError:
            return f"{provider.upper()}_API_KEY"
        return None

    # ---------- search ----------

    async def search(self, query: str, k: int = 8) -> list[SearchHit]:
        """Semantic search when available, merged with keyword (FTS5) hits."""
        hits: dict[int, SearchHit] = {}
        if self.index is not None:
            try:
                semantic = await asyncio.to_thread(
                    self.index.search, query, k, (), self.settings.rag_min_score
                )
                hits.update({h.message_id: h for h in semantic})
            except Exception as e:
                log.warning("Semantic search failed: %s", e)
        for h in await self.db.search_messages(query, limit=k):
            hits.setdefault(h.message_id, h.model_copy(update={"score": 0.0}))
        result = list(hits.values())[:k]
        return await self._attach_titles(result)

    async def _attach_titles(self, hits: list[SearchHit]) -> list[SearchHit]:
        titles = {t.id: t.title for t in await self.db.list_threads(limit=10_000)}
        return [
            h.model_copy(update={"thread_title": titles.get(h.thread_id)})
            for h in hits
            if h.thread_id in titles  # drop hits from deleted threads
        ]

    async def _retrieve(self, query: str, exclude_ids: list[int]) -> list[SearchHit]:
        if self.index is None or self.settings.rag_top_k <= 0:
            return []
        try:
            hits = await asyncio.to_thread(
                self.index.search,
                query,
                self.settings.rag_top_k,
                exclude_ids,
                self.settings.rag_min_score,
            )
        except Exception as e:
            log.warning("Context retrieval failed: %s", e)
            return []
        return await self._attach_titles(hits)

    # ---------- chat ----------

    async def send(
        self, thread_id: str, text: str, model: str | None = None
    ) -> AsyncIterator[ChatEvent]:
        """Run one chat turn, yielding events as they happen."""
        text = text.strip()
        if not text:
            raise ValueError("Message is empty")
        thread = await self.db.get_thread(thread_id)
        model = model or thread.model
        provider, full_model = self.router.resolve(model)
        self.settings.require_key(provider)  # fail before persisting anything

        is_first = not await self.db.get_messages(thread_id, limit=1)
        user_msg = await self.db.add_message(thread_id, "user", text)
        if is_first and thread.title in ("New chat", ""):
            await self.db.update_thread(thread_id, title=make_title(text), model=model)
        elif model != thread.model:
            await self.db.update_thread(thread_id, model=model)
        yield UserSaved(user_msg)

        history = await self.db.get_messages(thread_id, limit=self.settings.history_limit)
        retrieved = await self._retrieve(text, [m.id for m in history])
        prompt = assemble_prompt(history, retrieved, thread.system_prompt)

        parts: list[str] = []
        try:
            async for chunk in self.router.stream(prompt, model=model):
                if chunk.delta:
                    parts.append(chunk.delta)
                    yield Delta(thread_id, chunk.delta)
        except BaseException:
            if parts:  # keep what the user already saw
                await self.db.add_message(
                    thread_id, "assistant", "".join(parts) + INTERRUPTED_NOTE, provider, full_model
                )
            raise

        assistant_msg = await self.db.add_message(
            thread_id, "assistant", "".join(parts), provider, full_model
        )
        yield AssistantSaved(assistant_msg)
        await self._index([user_msg, assistant_msg])

    async def _index(self, messages: list[Message]) -> None:
        if self.index is None:
            return
        try:
            await asyncio.to_thread(self.index.add, messages)
        except Exception as e:  # backfill_index() will pick these up next start
            log.warning("Vector indexing failed: %s", e)
