"""MemoryService: the single entry point to the shared memory, used by every interface.

The MCP server, CLI, TUI and web UI all go through it: importing conversations from other AI
tools, saving notes, keyword + semantic search, and browsing stored threads.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable

from anamnesis.config import Settings
from anamnesis.core.db import Database
from anamnesis.core.models import (
    ImportedConversation,
    ImportedMessage,
    Message,
    SearchHit,
    Thread,
)
from anamnesis.core.vector import FastEmbedEmbedder, VectorIndex

log = logging.getLogger(__name__)

TITLE_CHARS = 60
# Shorter messages ("yes", "do it") carry no recallable meaning; they stay keyword-searchable.
MIN_INDEX_CHARS = 20


def make_title(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= TITLE_CHARS else line[: TITLE_CHARS - 1].rstrip() + "…"


class MemoryService:
    def __init__(self, db: Database, index: VectorIndex | None, settings: Settings) -> None:
        self.db = db
        self.index = index
        self.settings = settings

    # ---------- lifecycle ----------

    @classmethod
    async def create(cls, settings: Settings) -> MemoryService:
        """Open the database and (best-effort) the vector index from settings.

        Missing embeddings are filled in by the background memory sync (`ingest.runner.watch`)
        rather than here, so startup never waits on a large backlog.
        """
        db = await Database(settings.db_path).open()
        index: VectorIndex | None = None
        if settings.semantic_search:
            try:
                embedder = await asyncio.to_thread(
                    FastEmbedEmbedder, settings.embedding_model, settings.data_dir / "models"
                )
                index = await asyncio.to_thread(VectorIndex, settings.vector_path, embedder)
            except Exception as e:  # offline first run, corrupted cache, ...
                log.warning("Semantic memory disabled (embedder unavailable): %s", e)
        return cls(db, index, settings)

    async def close(self) -> None:
        await self.db.close()

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

    async def delete_thread(self, thread_id: str) -> None:
        await self.db.delete_thread(thread_id)
        if self.index is not None:
            try:
                await asyncio.to_thread(self.index.delete_thread, thread_id)
            except Exception as e:
                log.warning("Failed to remove thread %s from vector index: %s", thread_id, e)

    async def wipe(self) -> None:
        """Forget everything (a node being reset). Irreversible."""
        await self.db.wipe()
        if self.index is not None:
            await asyncio.to_thread(self.index.clear)

    async def messages(
        self, thread_id: str, limit: int | None = None, before: int | None = None
    ) -> list[Message]:
        return await self.db.get_messages(thread_id, limit=limit, before=before)

    # ---------- change feed (live updates in the TUI and web UI) ----------

    async def latest_message_id(self) -> int:
        return await self.db.max_message_id()

    async def messages_after(self, after_id: int) -> list[Message]:
        return await self.db.messages_after(after_id)

    async def threads_signature(self) -> tuple[int, float, int]:
        return await self.db.threads_signature()

    # ---------- search ----------

    async def search(
        self,
        query: str,
        k: int = 8,
        source: str | None = None,
        project: str | None = None,
    ) -> list[SearchHit]:
        """Semantic search when available, merged with keyword (FTS5) hits.

        Results can be limited to one source (e.g. "claude-code") and/or project directory.
        """
        filtered = bool(source or project)
        hits: dict[int, SearchHit] = {}
        if self.index is not None:
            try:
                # Oversample when filtering: the vector store doesn't know thread metadata.
                semantic = await asyncio.to_thread(
                    self.index.search,
                    query,
                    k * 5 if filtered else k,
                    (),
                    self.settings.semantic_min_score,
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
            if (not source or h.source == source) and (not project or h.project == project)
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

    # ---------- writing: imports from other tools, notes ----------

    async def import_conversation(
        self, conv: ImportedConversation, index: bool = True, origin: str | None = None
    ) -> tuple[Thread, int]:
        """Store a conversation from another tool and (optionally) embed its new messages.

        Bulk imports pass `index=False` and call `backfill_index()` once afterwards, so text is
        searchable by keyword immediately and semantic recall fills in as embedding proceeds.
        """
        thread, added = await self.db.import_conversation(conv, origin=origin)
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
