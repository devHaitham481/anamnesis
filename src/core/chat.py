"""ChatService: the single orchestration point used by both the TUI and the web server.

Flow for one turn: persist user message -> retrieve related snippets -> assemble prompt ->
stream from the provider router -> persist assistant message -> index both in the vector store.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Literal

from src.config import MissingAPIKeyError, Settings
from src.core.context import assemble_prompt
from src.core.db import Database, ThreadNotFoundError
from src.core.models import (
    ImportedConversation,
    ImportedMessage,
    Message,
    SearchHit,
    Thread,
)
from src.core.vector import FastEmbedEmbedder, VectorIndex
from src.providers.router import ProviderRouter

log = logging.getLogger(__name__)

TITLE_CHARS = 60
# Shorter messages ("yes", "do it") carry no recallable meaning; they stay keyword-searchable.
MIN_INDEX_CHARS = 20


class ThreadBusyError(RuntimeError):
    """Raised when a second turn is started on a thread that is still streaming."""


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
        self._active: set[str] = set()

    # ---------- lifecycle ----------

    @classmethod
    async def create(
        cls, settings: Settings, router: ProviderRouter | None = None, backfill: bool = False
    ) -> ChatService:
        """Open the database and (best-effort) the vector index from settings.

        Missing embeddings are filled in by the background memory sync (`ingest.runner.watch`)
        rather than here, so startup never waits on a large backlog.
        """
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
        if backfill:
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

    async def backfill_index(
        self, batch: int = 64, progress: Callable[[int, int], None] | None = None
    ) -> int:
        """Embed every message not yet in the vector index (e.g. after a bulk import).

        Runs in small batches so a large first import never blocks other work for long and
        progress can be reported. Returns how many messages were indexed.
        """
        if self.index is None:
            return 0
        index = self.index
        indexed = await asyncio.to_thread(index.indexed_ids)
        missing = [m for m in await self.db.all_messages() if m.id not in indexed and _indexable(m)]
        done = 0
        for start in range(0, len(missing), batch):
            chunk = missing[start : start + batch]
            try:
                await asyncio.to_thread(index.add, chunk)
            except Exception as e:
                log.warning("Vector indexing failed: %s", e)
                break
            done += len(chunk)
            if progress is not None:
                progress(done, len(missing))
        return done

    # ---------- threads ----------

    async def new_thread(self, model: str | None = None, title: str = "New chat") -> Thread:
        model = model or self.default_model
        self.router.resolve(model)  # validate early
        return await self.db.create_thread(title=title, model=model)

    async def list_threads(
        self, limit: int = 200, source: str | None = None, project: str | None = None
    ) -> list[Thread]:
        return await self.db.list_threads(limit=limit, source=source, project=project)

    async def source_counts(self) -> dict[str, int]:
        return await self.db.source_counts()

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

    async def search(
        self,
        query: str,
        k: int = 8,
        source: str | None = None,
        project: str | None = None,
        exclude_thread: str | None = None,
    ) -> list[SearchHit]:
        """Semantic search when available, merged with keyword (FTS5) hits.

        Results can be limited to one source (e.g. "claude-code") and/or project directory.
        """
        filtered = bool(source or project or exclude_thread)
        hits: dict[int, SearchHit] = {}
        if self.index is not None:
            try:
                # Oversample when filtering: the vector store doesn't know thread metadata.
                semantic = await asyncio.to_thread(
                    self.index.search,
                    query,
                    k * 5 if filtered else k,
                    (),
                    self.settings.rag_min_score,
                )
                hits.update({h.message_id: h for h in semantic})
            except Exception as e:
                log.warning("Semantic search failed: %s", e)
        for h in await self.db.search_messages(query, limit=k * 3, source=source, project=project):
            hits.setdefault(h.message_id, h.model_copy(update={"score": 0.0}))
        result = await self._attach_titles(list(hits.values()))
        result = [
            h
            for h in result
            if (not source or h.source == source)
            and (not project or h.project == project)
            and h.thread_id != exclude_thread
        ]
        return result[:k]

    async def _attach_titles(self, hits: list[SearchHit]) -> list[SearchHit]:
        meta = await self.db.thread_meta(sorted({h.thread_id for h in hits}))
        out: list[SearchHit] = []
        for h in hits:
            t = meta.get(h.thread_id)
            if t is None:  # drop hits from deleted threads
                continue
            update: dict[str, object] = {
                "thread_title": t.title,
                "source": t.source,
                "project": t.project,
            }
            out.append(h.model_copy(update=update))
        missing = [h.message_id for h in out if h.created_at is None]
        if missing:
            created = {m.id: m.created_at for m in await self.db.get_messages_by_ids(missing)}
            out = [
                h
                if h.created_at is not None
                else h.model_copy(update={"created_at": created.get(h.message_id)})
                for h in out
            ]
        return out

    # ---------- shared memory: imports from other tools, notes ----------

    async def import_conversation(
        self, conv: ImportedConversation, index: bool = True, origin: str | None = None
    ) -> tuple[Thread, int]:
        """Store a conversation from another tool and (optionally) embed its new messages.

        Bulk imports pass `index=False` and call `backfill_index()` once afterwards, so text is
        searchable by keyword immediately and semantic recall fills in as embedding proceeds.
        """
        thread, added = await self.db.import_conversation(
            conv, model=self.default_model, origin=origin
        )
        if index:
            await self._index(added)
        return thread, len(added)

    async def remember(
        self, content: str, topic: str | None = None, source: str = "note"
    ) -> Thread:
        """Save a durable note (e.g. written by an agent through MCP) into shared memory."""
        content = content.strip()
        if not content:
            raise ValueError("Nothing to remember")
        topic = (topic or "General").strip()[:80] or "General"
        conv = ImportedConversation(
            source=source,
            external_id=topic.lower(),
            title=f"Notes: {topic}",
            messages=[
                ImportedMessage(
                    external_id=uuid.uuid4().hex,
                    role="user",
                    content=content,
                    created_at=time.time(),
                )
            ],
        )
        thread, _ = await self.import_conversation(conv)
        return thread

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

    def is_busy(self, thread_id: str) -> bool:
        return thread_id in self._active

    @property
    def active_threads(self) -> list[str]:
        return sorted(self._active)

    async def send(
        self, thread_id: str, text: str, model: str | None = None
    ) -> AsyncGenerator[ChatEvent, None]:
        """Run one chat turn, yielding events as they happen.

        Only one turn per thread may run at a time in this process (`ThreadBusyError`);
        all validation happens before anything is persisted.
        """
        text = text.strip()
        if not text:
            raise ValueError("Message is empty")
        thread = await self.db.get_thread(thread_id)
        model = model or thread.model
        provider, full_model = self.router.resolve(model)
        self.settings.require_key(provider)  # fail before persisting anything
        if thread_id in self._active:
            raise ThreadBusyError("This thread is already generating a response")

        self._active.add(thread_id)
        try:
            async with contextlib.aclosing(
                self._turn(thread, text, model, provider, full_model)
            ) as events:
                async for event in events:
                    yield event
        finally:
            self._active.discard(thread_id)

    async def _turn(
        self, thread: Thread, text: str, model: str, provider: str, full_model: str
    ) -> AsyncGenerator[ChatEvent, None]:
        thread_id = thread.id
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
            # aclosing: on cancel/disconnect the provider HTTP stream is closed right away
            # instead of generating (billed) tokens until garbage collection.
            async with contextlib.aclosing(self.router.stream(prompt, model=model)) as stream:
                async for chunk in stream:
                    if chunk.delta:
                        parts.append(chunk.delta)
                        yield Delta(thread_id, chunk.delta)
        except BaseException:
            if parts:  # keep what the user already saw
                with contextlib.suppress(ThreadNotFoundError):  # thread deleted mid-stream
                    await self.db.add_message(
                        thread_id,
                        "assistant",
                        "".join(parts) + INTERRUPTED_NOTE,
                        provider,
                        full_model,
                    )
            raise

        assistant_msg = await self.db.add_message(
            thread_id, "assistant", "".join(parts), provider, full_model
        )
        yield AssistantSaved(assistant_msg)
        await self._index([user_msg, assistant_msg])

    async def _index(self, messages: list[Message]) -> None:
        messages = [m for m in messages if _indexable(m)]
        if self.index is None or not messages:
            return
        try:
            await asyncio.to_thread(self.index.add, messages)
        except Exception as e:  # backfill_index() will pick these up next start
            log.warning("Vector indexing failed: %s", e)


def _indexable(message: Message) -> bool:
    return len(message.content.strip()) >= MIN_INDEX_CHARS
