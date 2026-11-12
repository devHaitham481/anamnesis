import time
from pathlib import Path

from anamnesis.core.models import Message
from anamnesis.core.vector import HashEmbedder, VectorIndex


def msg(i: int, text: str, thread: str = "t1") -> Message:
    return Message(id=i, thread_id=thread, role="user", content=text, created_at=0)


def test_add_search_exclude_delete(tmp_path: Path) -> None:
    idx = VectorIndex(tmp_path / "v", HashEmbedder())
    idx.add([msg(1, "postgres database choice"), msg(2, "pizza toppings"), msg(3, "", "t2")])
    assert idx.count() == 2  # empty content skipped
    hits = idx.search("which database", k=2)
    assert hits[0].message_id == 1 and hits[0].score > hits[-1].score
    assert all(h.message_id != 1 for h in idx.search("which database", exclude_ids=[1]))
    assert idx.search("which database", min_score=0.99) == []
    assert idx.indexed_ids() == {1, 2}
    idx.delete_thread("t1")
    assert idx.count() == 0
    assert idx.search("anything") == []


def test_thread_id_quoting(tmp_path: Path) -> None:
    idx = VectorIndex(tmp_path / "v", HashEmbedder())
    idx.add([msg(1, "x", thread="it's")])
    idx.delete_thread("it's")
    assert idx.count() == 0


def test_reopen_and_dimension_change(tmp_path: Path) -> None:
    VectorIndex(tmp_path / "v", HashEmbedder(64)).add([msg(1, "hello")])
    assert VectorIndex(tmp_path / "v", HashEmbedder(64)).count() == 1
    assert VectorIndex(tmp_path / "v", HashEmbedder(32)).count() == 0  # rebuilt


def test_search_latency_under_50ms(tmp_path: Path) -> None:
    idx = VectorIndex(tmp_path / "v", HashEmbedder())
    idx.add([msg(i, f"message {i} about subject {i % 13}") for i in range(1, 1001)])
    idx.search("warmup")
    start = time.perf_counter()
    idx.search("subject 7", k=5, exclude_ids=list(range(1, 41)))
    assert time.perf_counter() - start < 0.05
