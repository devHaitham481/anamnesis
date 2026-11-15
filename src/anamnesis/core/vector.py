"""Semantic search over chat messages: a local ONNX embedder and a SQLite + numpy vector index.

Kept deliberately light, because every `anam search` and every MCP server start pays for it:
- the embedder runs the ONNX model directly (onnxruntime + tokenizers, ~0.07 s to import),
  the same model and pooling fastembed used, so stored vectors stay comparable;
- vectors live in one SQLite file and are searched by brute-force cosine in numpy. Tens of
  thousands of messages take milliseconds, with no LanceDB/pyarrow import (~0.6 s).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np

from anamnesis.core.models import Message, SearchHit

log = logging.getLogger(__name__)

EMBED_CHARS = 1000
DB_FILE = "embeddings.db"

# Model name -> Hugging Face repo with its quantized ONNX export (as fastembed resolved it).
ONNX_REPOS = {"BAAI/bge-small-en-v1.5": "Qdrant/bge-small-en-v1.5-onnx-Q"}
MODEL_FILE = "model_optimized.onnx"


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


def _model_dir(repo: str, cache_dir: Path) -> Path:
    """The downloaded model, fetched once (Hugging Face cache layout, shared with fastembed)."""
    snapshots = cache_dir / f"models--{repo.replace('/', '--')}" / "snapshots"
    for snap in sorted(snapshots.glob("*")) if snapshots.is_dir() else []:
        if (snap / MODEL_FILE).is_file() and (snap / "tokenizer.json").is_file():
            return snap  # already here: no network at all
    for var, value in (
        ("HF_HUB_DISABLE_PROGRESS_BARS", "1"),
        ("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1"),
        ("HF_HUB_VERBOSITY", "error"),
    ):
        os.environ.setdefault(var, value)
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    from huggingface_hub import snapshot_download

    return Path(
        snapshot_download(repo, cache_dir=str(cache_dir), allow_patterns=["*.json", MODEL_FILE])
    )


class OnnxEmbedder:
    """Local sentence embeddings: BGE-small via onnxruntime, CLS pooling, L2-normalized."""

    def __init__(self, model_name: str, cache_dir: Path) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        repo = ONNX_REPOS.get(model_name)
        if repo is None:
            raise ValueError(f"Unsupported embedding model {model_name!r}")
        path = _model_dir(repo, cache_dir)
        config = json.loads((path / "config.json").read_text())
        self.dim = int(config["hidden_size"])
        tokenizer = Tokenizer.from_file(str(path / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=512)
        tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        self._tokenizer = tokenizer
        options = ort.SessionOptions()
        options.log_severity_level = 3
        self._session = ort.InferenceSession(
            str(path / MODEL_FILE), options, providers=["CPUExecutionProvider"]
        )
        self._inputs = {i.name for i in self._session.get_inputs()}
        self._lock = threading.Lock()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # Batches are padded to their longest text, so embed texts of similar length together.
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out: list[list[float]] = [[] for _ in texts]
        with self._lock:
            for start in range(0, len(order), 32):
                chunk = order[start : start + 32]
                for i, vec in zip(
                    chunk, self._batch([texts[i] for i in chunk]).tolist(), strict=True
                ):
                    out[i] = vec
        return out

    def _batch(self, texts: Sequence[str]) -> np.ndarray:
        encoded = self._tokenizer.encode_batch(list(texts))
        feeds = {
            "input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encoded], dtype=np.int64),
        }
        hidden = self._session.run(None, {k: v for k, v in feeds.items() if k in self._inputs})[0]
        cls = hidden[:, 0] if hidden.ndim == 3 else hidden
        norms = np.linalg.norm(cls, axis=1, keepdims=True)
        return cls / np.maximum(norms, 1e-12)  # type: ignore[no-any-return]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS vectors (
    message_id INTEGER PRIMARY KEY,
    thread_id  TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    vec        BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vectors_thread ON vectors(thread_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class VectorIndex:
    """Vector index in SQLite. Synchronous: call from async code via `asyncio.to_thread`.

    Several processes (the MCP server, the TUI, `anam search`) may share it: SQLite handles
    the writes, and each reader reloads its in-memory matrix only when the table changed.
    """

    def __init__(self, path: str | Path, embedder: Embedder) -> None:
        self.path = Path(path)
        self.embedder = embedder
        self.path.mkdir(parents=True, exist_ok=True)
        _drop_old_lancedb(self.path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path / DB_FILE, check_same_thread=False, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        dim = self._conn.execute("SELECT value FROM meta WHERE key = 'dim'").fetchone()
        if dim is not None and int(dim[0]) != embedder.dim:
            # Embedding model changed: the old vectors are unusable, rebuild from scratch.
            log.warning("Embedding dimension changed; recreating vector index")
            self._conn.execute("DELETE FROM vectors")
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('dim', ?)", (str(embedder.dim),)
        )
        self._conn.commit()
        self._cache_key: tuple[int, int] | None = None
        self._ids = np.zeros(0, dtype=np.int64)
        self._matrix = np.zeros((0, embedder.dim), dtype=np.float32)

    def warm(self) -> None:
        """Load the matrix and run the model once, so the first real search is fast."""
        with self._lock:
            self._load()
        self.embedder.embed(["warm up"])

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])

    def indexed_ids(self) -> set[int]:
        with self._lock:
            return {int(r[0]) for r in self._conn.execute("SELECT message_id FROM vectors")}

    def add(self, messages: Sequence[Message]) -> None:
        msgs = [m for m in messages if m.content.strip() and m.role != "system"]
        if not msgs:
            return
        # The opening of a message carries its topic; embedding cost grows steeply with length
        # (512 tokens is ~10x slower than 150 on CPU), so only the head is embedded.
        vectors = self.embedder.embed([m.content[:EMBED_CHARS] for m in msgs])
        rows = [
            (m.id, m.thread_id, m.role, m.content, np.asarray(v, dtype=np.float32).tobytes())
            for m, v in zip(msgs, vectors, strict=True)
        ]
        with self._lock:
            # Keyed by message id: indexing the same message twice never duplicates it.
            self._conn.executemany(
                "INSERT OR REPLACE INTO vectors (message_id, thread_id, role, content, vec) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def _load(self) -> None:
        """(Re)load the matrix if another writer changed the table since the last search."""
        key = self._conn.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM vectors").fetchone()
        if key == self._cache_key:
            return
        rows = self._conn.execute("SELECT message_id, vec FROM vectors").fetchall()
        dim = self.embedder.dim
        self._ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        blob = b"".join(r[1] for r in rows)
        self._matrix = np.frombuffer(blob, dtype=np.float32).reshape(len(rows), dim)
        self._cache_key = (int(key[0]), int(key[1]))

    def search(
        self,
        query: str,
        k: int = 5,
        exclude_ids: Sequence[int] = (),
        min_score: float | None = None,
    ) -> list[SearchHit]:
        """Cosine-similarity search. `score` is similarity in [-1, 1] (higher is closer)."""
        if not query.strip():
            return []
        with self._lock:
            self._load()
            if not len(self._ids):
                return []
            q = np.asarray(self.embedder.embed([query])[0], dtype=np.float32)
            scores = self._matrix @ q
            if exclude_ids:
                scores[np.isin(self._ids, np.asarray(exclude_ids, dtype=np.int64))] = -np.inf
            n = min(k, len(scores))
            top = np.argpartition(-scores, n - 1)[:n]
            top = top[np.argsort(-scores[top])]
            picked = [(int(self._ids[i]), float(scores[i])) for i in top if scores[i] > -np.inf]
            if min_score is not None:
                picked = [(mid, s) for mid, s in picked if s >= min_score]
            if not picked:
                return []
            marks = ",".join("?" * len(picked))
            rows = {
                r[0]: r
                for r in self._conn.execute(
                    f"SELECT message_id, thread_id, role, content FROM vectors "
                    f"WHERE message_id IN ({marks})",
                    [mid for mid, _ in picked],
                )
            }
        return [
            SearchHit(
                message_id=mid,
                thread_id=rows[mid][1],
                role=rows[mid][2],
                content=rows[mid][3],
                score=score,
            )
            for mid, score in picked
            if mid in rows
        ]

    def delete_thread(self, thread_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM vectors WHERE thread_id = ?", (thread_id,))
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM vectors")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _drop_old_lancedb(path: Path) -> None:
    """Earlier versions kept vectors in LanceDB; they are re-embedded into SQLite instead."""
    old = path / "messages.lance"
    if old.is_dir():
        shutil.rmtree(old, ignore_errors=True)
        log.info("Removed the old LanceDB vector index; messages will be re-embedded")
