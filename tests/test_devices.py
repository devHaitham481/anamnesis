"""Cross-device memory sync: two simulated computers sharing one synced folder."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from anamnesis.config import Settings
from anamnesis.core.devices import (
    SyncSetupError,
    decrypt,
    encode_code,
    exchange,
    join,
    list_devices,
    load_config,
    share,
    this_device,
)
from anamnesis.core.memory import MemoryService
from anamnesis.core.models import ImportedConversation, ImportedMessage
from tests.conftest import open_service


async def make_service(root: Path, name: str) -> MemoryService:
    settings = Settings(data_dir=root / name, memory_sync=False, semantic_min_score=0.3)
    return await open_service(settings)


@pytest.fixture
async def laptops(tmp_path: Path) -> AsyncIterator[tuple[MemoryService, MemoryService, Path]]:
    a = await make_service(tmp_path, "laptop-a")
    b = await make_service(tmp_path, "laptop-b")
    folder = tmp_path / "iCloud" / "Anamnesis"
    yield a, b, folder
    await a.close()
    await b.close()


def session(ext: str, *texts: str) -> ImportedConversation:
    return ImportedConversation(
        source="claude-code",
        external_id=ext,
        title=f"Session {ext}",
        project="/work/api",
        messages=[
            ImportedMessage(
                external_id=f"{ext}-{i}",
                role="user" if i % 2 == 0 else "assistant",
                content=t,
                created_at=1_790_000_000 + i,
            )
            for i, t in enumerate(texts)
        ],
    )


def pair(a: MemoryService, b: MemoryService, folder: Path) -> str:
    code = share(a.settings.data_dir, folder)
    join(b.settings.data_dir, folder, code)
    return code


async def test_pairing_rules(laptops: tuple[MemoryService, MemoryService, Path]) -> None:
    a, b, folder = laptops
    with pytest.raises(SyncSetupError, match="No Anamnesis memory"):
        join(b.settings.data_dir, folder, "A" * 52)
    code = share(a.settings.data_dir, folder)
    assert share(a.settings.data_dir, folder) == code  # idempotent on the same device
    with pytest.raises(SyncSetupError, match="already holds"):
        share(b.settings.data_dir, folder)  # second computer must join, not re-create
    with pytest.raises(SyncSetupError, match="does not match"):
        join(b.settings.data_dir, folder, encode_code(b"\x01" * 32))  # valid, wrong key
    with pytest.raises(SyncSetupError, match="not valid"):
        join(b.settings.data_dir, folder, "not-a-code")
    join(b.settings.data_dir, folder, code.lower().replace("-", " "))  # forgiving input
    cfg = load_config(b.settings.data_dir)
    assert cfg is not None and cfg.folder == folder
    assert (b.settings.data_dir / "sync.json").stat().st_mode & 0o077 == 0  # key file private
    assert not any(
        code.replace("-", "") in p.read_text(errors="ignore")
        for p in folder.rglob("*")
        if p.is_file()
    )  # key never in the folder


async def test_memory_flows_both_ways_without_echo_or_duplicates(
    laptops: tuple[MemoryService, MemoryService, Path],
) -> None:
    a, b, folder = laptops
    pair(a, b, folder)
    await a.import_conversation(session("s1", "how do we rotate the api keys", "use vault"))
    # A chat from the old built-in chat (no external id), as found in existing databases.
    chat = await a.db.create_thread("Planning")
    await a.db.add_message(chat.id, "user", "remember we ship on fridays")
    await a.remember("Prefers pnpm over npm.", topic="preferences")

    first = await exchange(a)
    assert first.exported == 4 and first.errors == []
    assert (await exchange(a)).exported == 0  # nothing new

    got = await exchange(b)
    assert got.imported == 4 and sum(got.from_devices.values()) == 4
    counts = await b.source_counts()
    assert counts == {"claude-code": 1, "anamnesis": 1, "note": 1}
    assert [h.content for h in await b.search("rotate api keys", k=1)] == [
        "how do we rotate the api keys"
    ]
    assert (await exchange(b)).imported == 0  # idempotent
    assert (await exchange(b)).exported == 0  # received memory is never echoed back

    # B adds to the same legacy chat and the same note topic.
    b_chat = next(t for t in await b.list_threads() if t.source == "anamnesis")
    await b.db.add_message(b_chat.id, "assistant", "noted: fridays it is")
    await b.remember("Deploys only via CI.", topic="Preferences")
    assert (await exchange(b)).exported == 2

    back = await exchange(a)
    assert back.imported == 2
    # merged into the SAME threads on A, not duplicated
    assert await a.source_counts() == {"claude-code": 1, "anamnesis": 1, "note": 1}
    assert [m.content for m in await a.messages(chat.id)] == [
        "remember we ship on fridays",
        "noted: fridays it is",
    ]
    notes = next(t for t in await a.list_threads() if t.source == "note")
    assert len(await a.messages(notes.id)) == 2
    assert (await exchange(a)).exported == 0


async def test_new_computer_gets_everything_and_live_updates(
    laptops: tuple[MemoryService, MemoryService, Path], tmp_path: Path
) -> None:
    a, b, folder = laptops
    pair(a, b, folder)
    await a.import_conversation(session("s1", "first fact about the billing service"))
    await b.import_conversation(session("s2", "second fact about the search service"))
    await exchange(a)
    await exchange(b)
    c = await make_service(tmp_path, "new-mac")
    try:
        join(c.settings.data_dir, folder, share(a.settings.data_dir, folder))
        report = await exchange(c)
        assert report.imported == 2 and report.errors == []
        assert len(list_devices(load_config(c.settings.data_dir))) == 3  # type: ignore[arg-type]
        await a.import_conversation(
            session("s1", "first fact about the billing service", "a later follow-up on billing")
        )
        await exchange(a)
        assert (await exchange(c)).imported == 1  # appended to the existing session
    finally:
        await c.close()


async def test_waits_for_missing_segments_and_survives_bad_files(
    laptops: tuple[MemoryService, MemoryService, Path],
) -> None:
    a, b, folder = laptops
    pair(a, b, folder)
    a_id = this_device(a.settings.data_dir).id
    await a.import_conversation(session("s1", "segment one content"))
    await exchange(a)
    await a.import_conversation(session("s2", "segment two content"))
    await exchange(a)
    seg1 = folder / "anamnesis-memory" / a_id / "000001.nxs"
    seg2 = folder / "anamnesis-memory" / a_id / "000002.nxs"
    assert b"segment one" not in seg1.read_bytes()  # encrypted at rest
    hidden = seg1.with_name("held-back")
    seg1.rename(hidden)  # e.g. iCloud hasn't delivered file 1 yet
    assert (await exchange(b)).imported == 0  # never skips a gap
    hidden.rename(seg1)
    assert (await exchange(b)).imported == 2

    seg2.write_bytes(seg2.read_bytes()[:-5] + b"xxxxx")  # corrupted / tampered in the cloud
    d = await make_service(folder.parent, "fresh-mac")
    try:
        join(d.settings.data_dir, folder, share(a.settings.data_dir, folder))
        report = await exchange(d)
        assert report.imported == 1  # segment 1 only
        assert report.errors and "cannot decrypt" in report.errors[0]
        assert [t.external_id for t in await d.list_threads()] == ["s1"]
        again = await exchange(d)  # stays parked on the bad segment, never skips past it
        assert again.imported == 0 and again.errors
    finally:
        await d.close()
    with pytest.raises(ValueError, match="decrypt"):
        decrypt(b"\0" * 32, a_id, seg1.read_bytes())


async def test_unreachable_folder_is_reported_not_fatal(
    laptops: tuple[MemoryService, MemoryService, Path],
) -> None:
    a, b, folder = laptops
    pair(a, b, folder)
    import shutil

    shutil.rmtree(folder)
    report = await exchange(a)
    assert report.errors and "not available" in report.errors[0]
