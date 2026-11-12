"""Shared-memory layer: imports, migration, filtered search, background sync, MCP, connect."""

from __future__ import annotations

import json
import sqlite3
import tomllib
from pathlib import Path

import pytest

from anamnesis.core.db import Database
from anamnesis.core.memory import MemoryService
from anamnesis.core.models import ImportedConversation, ImportedMessage
from anamnesis.ingest.claude_code import ClaudeCodeSource
from anamnesis.ingest.runner import scan, sync_lock, sync_once
from anamnesis.memory_mcp import server as mcp_server


def conv(
    ext: str,
    *texts: str,
    source: str = "claude-code",
    project: str | None = None,
    title: str | None = None,
) -> ImportedConversation:
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
                created_at=1_700_000_000 + i,
            )
            for i, t in enumerate(texts)
        ],
    )


async def test_import_is_idempotent_and_appends_only_new(service: MemoryService) -> None:
    t1, n1 = await service.import_conversation(conv("s1", "first question here", "an answer"))
    assert n1 == 2 and t1.source == "claude-code" and t1.title == "first question here"
    _, again = await service.import_conversation(conv("s1", "first question here", "an answer"))
    assert again == 0
    t2, grown = await service.import_conversation(
        conv("s1", "first question here", "an answer", "follow-up question", title="Named")
    )
    assert grown == 1 and t2.id == t1.id and t2.title == "Named"
    assert len(await service.messages(t1.id)) == 3
    assert t2.updated_at == pytest.approx(1_700_000_002)
    # same external id from another tool is a different conversation
    t3, _ = await service.import_conversation(conv("s1", "other tool text", source="codex"))
    assert t3.id != t1.id


async def test_search_filters_by_source_and_project(service: MemoryService) -> None:
    await service.import_conversation(
        conv("a", "postgres migration plan for billing", source="claude-code", project="/p/api")
    )
    await service.import_conversation(
        conv("b", "postgres tuning tips for analytics", source="chatgpt")
    )
    everything = await service.search("postgres", k=10)
    assert {h.source for h in everything} == {"claude-code", "chatgpt"}
    assert all(h.thread_title and h.created_at for h in everything)
    only_gpt = await service.search("postgres", k=10, source="chatgpt")
    assert {h.source for h in only_gpt} == {"chatgpt"}
    in_project = await service.search("postgres", k=10, project="/p/api")
    assert {h.project for h in in_project} == {"/p/api"}
    threads = await service.list_threads(source="chatgpt")
    assert [t.source for t in threads] == ["chatgpt"]
    assert await service.source_counts() == {"claude-code": 1, "chatgpt": 1}


async def test_keyword_search_ignores_filler_words(service: MemoryService) -> None:
    await service.import_conversation(conv("a", "why did we do this and what was it for"))
    await service.import_conversation(conv("b", "we picked lancedb because it is embedded"))
    hits = await service.db.search_messages("why did we pick lancedb", limit=5)
    assert [h.content for h in hits] == ["we picked lancedb because it is embedded"]


async def test_remember_appends_to_topic_note(service: MemoryService) -> None:
    t1 = await service.remember("User prefers pnpm over npm in all repos.", topic="preferences")
    t2 = await service.remember("Deploys go through GitHub Actions only.", topic="Preferences")
    assert t1.id == t2.id and t1.source == "note" and t1.title == "Notes: preferences"
    assert len(await service.messages(t1.id)) == 2
    with pytest.raises(ValueError):
        await service.remember("   ")


async def test_short_messages_are_not_embedded(service: MemoryService) -> None:
    thread, _ = await service.import_conversation(
        conv("s", "yes", "a substantive answer about caching layers"), index=True
    )
    assert service.index is not None
    ids = service.index.indexed_ids()
    msgs = await service.messages(thread.id)
    assert {m.id for m in msgs if len(m.content) >= 20} == ids
    assert await service.backfill_index() == 0  # short ones are skipped, not retried as work


async def test_old_database_is_migrated_in_place(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL,
            system_prompt TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL
            REFERENCES threads(id) ON DELETE CASCADE, role TEXT NOT NULL, content TEXT NOT NULL,
            provider TEXT, model TEXT, created_at REAL NOT NULL);
        INSERT INTO threads VALUES ('t1', 'Old chat', 'claude', NULL, 1, 2);
        INSERT INTO messages(thread_id, role, content, created_at) VALUES ('t1','user','hi',1);
        """
    )
    old.commit()
    old.close()
    async with Database(path) as db:
        (t,) = await db.list_threads()
        assert (t.title, t.source, t.project) == ("Old chat", "anamnesis", None)
        assert [m.content for m in await db.get_messages("t1")] == ["hi"]
        thread, added = await db.import_conversation(conv("x", "imported text"))
        assert len(added) == 1 and thread.source == "claude-code"
    async with Database(path) as db:  # reopening an already-migrated db is a no-op
        assert len(await db.list_threads()) == 2


# ---------- scanning ----------


def write_session(root: Path, name: str, *texts: str) -> Path:
    path = root / "-proj" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for i, t in enumerate(texts):
            fh.write(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": f"{name}-{t}-{i}",
                        "cwd": "/proj",
                        "timestamp": "2026-11-01T10:00:00Z",
                        "message": {"role": "user", "content": t},
                    }
                )
                + "\n"
            )
    return path


async def test_scan_is_incremental(service: MemoryService, tmp_path: Path) -> None:
    root = tmp_path / "projects"
    path = write_session(root, "s1", "how do we deploy the api service")
    sources = [ClaudeCodeSource(root)]
    first = await scan(service, sources)
    assert (first.files_parsed, first.messages_added) == (1, 1)
    unchanged = await scan(service, sources)
    assert (unchanged.files_parsed, unchanged.messages_added) == (0, 0)
    write_session(root, "s1", "and how do we roll it back safely")
    grown = await scan(service, sources)
    assert (grown.files_parsed, grown.messages_added) == (1, 1)
    assert grown.by_source == {"claude-code": 1}
    assert service.index is not None and len(service.index.indexed_ids()) == 2
    path.unlink()
    assert (await scan(service, sources)).errors == []


async def test_scan_survives_a_broken_source(service: MemoryService, tmp_path: Path) -> None:
    class Broken:
        name = "broken"

        def discover(self) -> list[Path]:
            raise PermissionError("nope")

        def parse(self, path: Path):  # type: ignore[no-untyped-def]
            return []

    root = tmp_path / "projects"
    write_session(root, "s1", "a perfectly good conversation")
    report = await scan(service, [Broken(), ClaudeCodeSource(root)])
    assert report.messages_added == 1 and report.errors == ["broken: nope"]


async def test_sync_lock_is_exclusive(service: MemoryService, tmp_path: Path) -> None:
    root = tmp_path / "projects"
    write_session(root, "s1", "some history worth keeping")
    with sync_lock(service.settings.data_dir) as first:
        assert first
        with sync_lock(service.settings.data_dir) as second:
            assert not second
        # another process (simulated) is syncing: this pass is skipped
        assert (await sync_once(service, [ClaudeCodeSource(root)])).messages_added == 0
    assert (await sync_once(service, [ClaudeCodeSource(root)])).messages_added == 1


# ---------- MCP ----------


def _text(result: object) -> str:
    return "\n".join(c.text for c in result.content)  # type: ignore[attr-defined]


async def test_mcp_tools(service: MemoryService) -> None:
    thread, _ = await service.import_conversation(
        conv(
            "s1",
            "we chose lancedb for local vector search",
            "because it is embedded",
            project="/p/anamnesis",
            title="Vector store",
        )
    )
    server = mcp_server.build_server(service=service, sync=False)
    tools = {t.name for t in await server.list_tools()}
    assert tools == {"search_memory", "recent_conversations", "get_conversation", "remember"}
    mcp_server._state["service"] = service
    try:
        found = _text(await server.call_tool("search_memory", {"query": "lancedb"}))
        assert "Claude Code · /p/anamnesis" in found and "Vector store" in found
        assert thread.id in found
        none = _text(
            await server.call_tool("search_memory", {"query": "lancedb", "source": "chatgpt"})
        )
        assert none == "No matching memories."
        recent = _text(await server.call_tool("recent_conversations", {}))
        assert "Vector store" in recent
        convo = _text(await server.call_tool("get_conversation", {"thread_id": thread.id}))
        assert "because it is embedded" in convo
        saved = _text(
            await server.call_tool("remember", {"note": "Use uv, not pip.", "topic": "anamnesis"})
        )
        assert "Notes: anamnesis" in saved
        assert "Use uv" in _text(await server.call_tool("search_memory", {"query": "uv pip"}))
    finally:
        mcp_server._state.pop("service", None)


async def test_mcp_server_over_stdio_like_a_real_client(tmp_path: Path) -> None:
    """Spawn `python -m anamnesis.memory_mcp` exactly as Claude Code / Cursor would and talk MCP."""
    import sys

    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    data = tmp_path / "data"
    async with Database(data / "anamnesis.db") as db:
        await db.import_conversation(
            conv("s1", "the staging database lives in eu-west-1", title="Infra notes")
        )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "anamnesis.memory_mcp"],
        cwd=str(tmp_path),  # not the project dir: must work from anywhere
        env={
            "ANAMNESIS_DATA_DIR": str(data),
            "ANAMNESIS_SEMANTIC_SEARCH": "false",
            "ANAMNESIS_MEMORY_SYNC": "false",
        },
    )
    errlog = (tmp_path / "stderr.log").open("w")
    async with (
        stdio_client(params, errlog=errlog) as (read, write),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        assert init.server_info.name == "anamnesis-memory"
        assert "search_memory" in (init.instructions or "")
        names = {t.name for t in (await session.list_tools()).tools}
        assert names == {"search_memory", "recent_conversations", "get_conversation", "remember"}
        result = await session.call_tool("search_memory", {"query": "staging database"})
        text = "\n".join(c.text for c in result.content)  # type: ignore[union-attr]
        assert "eu-west-1" in text and "Infra notes" in text
        await session.call_tool("remember", {"note": "Prod runs on Fridays only."})
    errlog.close()
    async with Database(data / "anamnesis.db") as db:
        assert await db.source_counts() == {"claude-code": 1, "note": 1}


# ---------- connect ----------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_connect_json_tools_merge_and_back_up(fake_home: Path) -> None:
    from anamnesis.connect import targets

    gemini = fake_home / ".gemini" / "settings.json"
    gemini.parent.mkdir()
    gemini.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "x"}}}))
    t = targets()
    assert t["gemini-cli"].installed() and not t["gemini-cli"].connected()
    t["gemini-cli"].connect()
    data = json.loads(gemini.read_text())
    assert data["theme"] == "dark" and "other" in data["mcpServers"]
    assert data["mcpServers"]["anamnesis"]["args"] == ["-m", "anamnesis.memory_mcp"]
    assert Path(data["mcpServers"]["anamnesis"]["command"]).is_absolute()
    assert json.loads(gemini.with_name("settings.json.bak").read_text())["theme"] == "dark"
    assert t["gemini-cli"].connected()
    t["gemini-cli"].disconnect()
    assert "anamnesis" not in json.loads(gemini.read_text())["mcpServers"]

    cursor = fake_home / ".cursor" / "mcp.json"
    t["cursor"].connect()  # creates the file when missing
    assert "anamnesis" in json.loads(cursor.read_text())["mcpServers"]


def test_connect_codex_toml(fake_home: Path) -> None:
    from anamnesis.connect import targets

    cfg = fake_home / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('model = "gpt-5.5"\n\n[mcp_servers.other]\ncommand = "x"\n')
    codex = targets()["codex"]
    codex.connect()
    parsed = tomllib.loads(cfg.read_text())
    assert parsed["model"] == "gpt-5.5" and {"other", "anamnesis"} <= set(parsed["mcp_servers"])
    assert codex.connect() == "already connected"
    codex.disconnect()
    parsed = tomllib.loads(cfg.read_text())
    assert set(parsed["mcp_servers"]) == {"other"}


def test_connect_repairs_entries_from_an_old_install(fake_home: Path) -> None:
    """After a reinstall, rename or move, connect points tools at this install again."""
    from anamnesis.connect import targets

    stale = {"command": "/old/.venv/bin/python", "args": ["-m", "src.memory_mcp"]}
    gemini = fake_home / ".gemini" / "settings.json"
    gemini.parent.mkdir()
    gemini.write_text(json.dumps({"mcpServers": {"anamnesis": stale}}))
    cfg = fake_home / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text(
        'model = "m"\n\n[mcp_servers.anamnesis]\ncommand = "/old/python"\n'
        'args = ["-m", "src.memory_mcp"]\n\n[mcp_servers.other]\ncommand = "x"\n'
    )
    t = targets()
    for name in ("gemini-cli", "codex"):
        assert t[name].connected() and not t[name].current()
        assert t[name].connect().startswith("updated")
        assert t[name].current() and t[name].connect() == "already connected"
    entry = json.loads(gemini.read_text())["mcpServers"]["anamnesis"]
    assert entry["args"] == ["-m", "anamnesis.memory_mcp"]
    parsed = tomllib.loads(cfg.read_text())
    assert parsed["model"] == "m" and set(parsed["mcp_servers"]) == {"other", "anamnesis"}
    assert parsed["mcp_servers"]["anamnesis"]["args"] == ["-m", "anamnesis.memory_mcp"]
    assert "src.memory_mcp" in cfg.with_name("config.toml.bak").read_text()  # original kept


def test_refuses_to_clobber_invalid_config(fake_home: Path) -> None:
    from anamnesis.connect import targets

    cfg = fake_home / ".cursor" / "mcp.json"
    cfg.parent.mkdir()
    cfg.write_text("[1, 2]")
    with pytest.raises(ValueError):
        targets()["cursor"].connect()
    assert cfg.read_text() == "[1, 2]"
