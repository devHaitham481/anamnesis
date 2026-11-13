"""Local LanceDB vector index over chat messages, with pluggable embedders."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import threading
import warnings
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

import lancedb
import pyarrow as pa

from anamnesis.core.models import Message, SearchHit

log = logging.getLogger(__name__)

TABLE = "messages"
EMBED_CHARS = 1000


class Embedder(Protocol):
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Dependency-free bag-of-words hashing embedder (deterministic; for tests/offline use)."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in re.findall(r"\w+", text.lower()):
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
                vec[h % self.dim] += 1.0 if (h >> 32) & 1 else -1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


class FastEmbedEmbedder:
    """Local ONNX sentence embeddings via fastembed (model downloaded once into cache_dir)."""

    def __init__(self, model_name: str, cache_dir: Path | None = None) -> None:
        # The model is public: no Hugging Face token needed, and no download chatter in the CLI.
        for var, value in (
            ("HF_HUB_DISABLE_PROGRESS_BARS", "1"),
            ("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1"),
            ("HF_HUB_VERBOSITY", "error"),
        ):
            os.environ.setdefault(var, value)
        logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
        # fastembed re-enables progress bars, which warns because the env var above wins.
        warnings.filterwarnings("ignore", message="Cannot enable progress bars")
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name, cache_dir=str(cache_dir) if cache_dir else None)
        self.dim = len(next(iter(self._model.embed(["dimension probe"]))))
        self._lock = threading.Lock()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        with self._lock:
            return [v.tolist() for v in self._model.embed(list(texts), batch_size=32)]


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class VectorIndex:
    """Synchronous LanceDB wrapper. Call from async code via `asyncio.to_thread`."""

    def __init__(self, path: str | Path, embedder: Embedder) -> None:
        self.path = Path(path)
        self.embedder = embedder
        self.path.mkdir(parents=True, exist_ok=True)
        # Re-check the table version on every read so writes from the other process
        # (TUI <-> web server) are visible immediately.
        self._db = lancedb.connect(str(self.path), read_consistency_interval=timedelta(0))
        self._schema = pa.schema(
            [
                ("message_id", pa.int64()),
                ("thread_id", pa.string()),
                ("role", pa.string()),
                ("content", pa.string()),
                ("vector", pa.list_(pa.float32(), embedder.dim)),
            ]
        )
        self._table = self._open_table()

    def _open_table(self) -> Any:
        try:
            table = self._db.open_table(TABLE)
        except (FileNotFoundError, ValueError):
            return self._db.create_table(TABLE, schema=self._schema, exist_ok=True)
        existing = table.schema.field("vector").type
        if getattr(existing, "list_size", None) != self.embedder.dim:
            # Embedding model changed: the old vectors are unusable, rebuild from scratch.
            log.warning("Embedding dimension changed; recreating vector index")
            self._db.drop_table(TABLE)
            return self._db.create_table(TABLE, schema=self._schema)
        return table

    def count(self) -> int:
        return int(self._table.count_rows())

    def indexed_ids(self) -> set[int]:
        arrow = self._table.to_arrow().select(["message_id"])
        return {int(x) for x in arrow.column("message_id").to_pylist()}

    def add(self, messages: Sequence[Message]) -> None:
        msgs = [m for m in messages if m.content.strip() and m.role != "system"]
        if not msgs:
            return
        # The opening of a message carries its topic; embedding cost grows steeply with length
        # (512 tokens is ~10x slower than 150 on CPU), so only the head is embedded.
        vectors = self.embedder.embed([m.content[:EMBED_CHARS] for m in msgs])
        rows = [
            {
                "message_id": m.id,
                "thread_id": m.thread_id,
                "role": m.role,
                "content": m.content,
                "vector": v,
            }
            for m, v in zip(msgs, vectors, strict=True)
        ]
        # Upsert keyed by message id: indexing the same message twice (e.g. a backfill racing
        # a live turn in another process) never creates duplicates.
        (
            self._table.merge_insert("message_id")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(rows)
        )

    def search(
        self,
        query: str,
        k: int = 5,
        exclude_ids: Sequence[int] = (),
        min_score: float | None = None,
    ) -> list[SearchHit]:
        """Cosine-similarity search. `score` is similarity in [-1, 1] (higher is closer)."""
        if not query.strip() or self.count() == 0:
            return []
        vec = self.embedder.embed([query])[0]
        q = self._table.search(vec).distance_type("cosine").limit(k)
        if exclude_ids:
            q = q.where(f"message_id NOT IN ({', '.join(str(int(i)) for i in exclude_ids)})")
        hits: list[SearchHit] = []
        seen: set[int] = set()
        for r in q.to_list():
            mid = int(r["message_id"])
            if mid in seen:  # defensive: tolerate duplicates written by older versions
                continue
            seen.add(mid)
            hits.append(
                SearchHit(
                    message_id=mid,
                    thread_id=r["thread_id"],
                    role=r["role"],
                    content=r["content"],
                    score=1.0 - float(r["_distance"]),
                )
            )
        if min_score is not None:
            hits = [h for h in hits if h.score >= min_score]
        return hits

    def delete_thread(self, thread_id: str) -> None:
        self._table.delete(f"thread_id = {_sql_str(thread_id)}")

    def clear(self) -> None:
        self._table.delete("message_id >= 0")
