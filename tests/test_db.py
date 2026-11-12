import time
from pathlib import Path

import pytest

from src.core.db import Database, ThreadNotFoundError


@pytest.fixture
async def db(tmp_path: Path):  # type: ignore[no-untyped-def]
    async with Database(tmp_path / "t.db") as d:
        yield d


async def test_thread_crud(db: Database) -> None:
    t = await db.create_thread("Hello")
    assert (await db.get_thread(t.id)).title == "Hello"
    renamed = await db.update_thread(t.id, title="Renamed")
    assert renamed.title == "Renamed" and renamed.source == "anamnesis"
    assert [x.id for x in await db.list_threads()] == [t.id]
    await db.delete_thread(t.id)
    with pytest.raises(ThreadNotFoundError):
        await db.get_thread(t.id)
    with pytest.raises(ThreadNotFoundError):
        await db.delete_thread(t.id)


async def test_messages_order_limit_and_cascade(db: Database) -> None:
    t = await db.create_thread("x")
    for i in range(5):
        await db.add_message(t.id, "user" if i % 2 == 0 else "assistant", f"m{i}")
    assert [m.content for m in await db.get_messages(t.id)] == ["m0", "m1", "m2", "m3", "m4"]
    assert [m.content for m in await db.get_messages(t.id, limit=2)] == ["m3", "m4"]
    first = (await db.get_messages(t.id))[0]
    assert [m.content for m in await db.messages_after(first.id)] == ["m1", "m2", "m3", "m4"]
    assert await db.max_message_id() == first.id + 4
    await db.delete_thread(t.id)
    assert await db.get_messages(t.id) == []
    assert await db.search_messages("m1") == []  # FTS cleaned up via trigger


async def test_add_message_to_missing_thread(db: Database) -> None:
    with pytest.raises(ThreadNotFoundError):
        await db.add_message("nope", "user", "hi")


async def test_thread_ordering_by_activity(db: Database) -> None:
    a = await db.create_thread("a")
    b = await db.create_thread("b")
    await db.add_message(a.id, "user", "bump")
    assert [t.title for t in await db.list_threads()] == ["a", "b"]
    assert b.id in {t.id for t in await db.list_threads()}


async def test_keyword_search_is_injection_safe(db: Database) -> None:
    t = await db.create_thread("Infra")
    await db.add_message(t.id, "user", "We picked PostgreSQL for production")
    await db.add_message(t.id, "assistant", "Bananas are yellow")
    hits = await db.search_messages("postgres")
    assert [h.content for h in hits] == ["We picked PostgreSQL for production"]
    assert hits[0].thread_title == "Infra"
    for weird in ['"', "AND OR NOT", "*", "col:val", "()", ""]:
        await db.search_messages(weird)  # must not raise


async def test_threads_signature_changes(db: Database) -> None:
    s1 = await db.threads_signature()
    t = await db.create_thread("x")
    s2 = await db.threads_signature()
    await db.update_thread(t.id, title="y")
    assert len({s1, s2, await db.threads_signature()}) == 3


async def test_persistence_across_connections(tmp_path: Path) -> None:
    async with Database(tmp_path / "p.db") as d:
        t = await d.create_thread("keep")
        await d.add_message(t.id, "user", "persisted")
    async with Database(tmp_path / "p.db") as d:
        assert [m.content for m in await d.get_messages(t.id)] == ["persisted"]


async def test_lookup_latency_under_50ms(db: Database) -> None:
    t = await db.create_thread("x")
    for i in range(500):
        await db.conn.execute(
            "INSERT INTO messages(thread_id, role, content, created_at) VALUES (?, 'user', ?, 0)",
            (t.id, f"message number {i} about topic {i % 17}"),
        )
    await db.conn.commit()
    start = time.perf_counter()
    await db.get_messages(t.id, limit=40)
    await db.search_messages("topic 3")
    await db.list_threads()
    assert time.perf_counter() - start < 0.05
