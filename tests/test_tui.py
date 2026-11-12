"""Headless TUI tests via Textual's pilot, against a real MemoryService."""

import asyncio

from textual.widgets import Input, ListView

from anamnesis.cli.app import AnamnesisApp
from anamnesis.cli.widgets import MessageView, ThreadItem
from anamnesis.core.memory import MemoryService
from tests.conftest import conversation


async def wait_until(pilot, cond, limit: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + limit
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await pilot.pause(0.05)


def views(app: AnamnesisApp) -> list[MessageView]:
    return list(app.query(MessageView))


def ready(app: AnamnesisApp) -> bool:
    return not app.query_one("#prompt", Input).disabled


async def submit(pilot, app: AnamnesisApp, text: str) -> None:  # type: ignore[no-untyped-def]
    prompt = app.query_one("#prompt", Input)
    prompt.value = text
    await pilot.press("enter")


async def test_opens_latest_conversation_read_only(service: MemoryService) -> None:
    await service.import_conversation(conversation("old", "older session", start=1_000))
    t, _ = await service.import_conversation(
        conversation("new", "how do we deploy", "via CI", title="Deploys", project="/w/api")
    )
    app = AnamnesisApp(service.settings, service)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: app.thread_id == t.id and len(views(app)) == 2)
        assert [v.content for v in views(app)] == ["how do we deploy", "via CI"]
        assert views(app)[0].header.startswith("You · ")
        assert app.sub_title == "Deploys · Claude Code · /w/api"
        assert [i.has_class("current") for i in app.query(ThreadItem)] == [True, False]
        assert "2 conversations from 1 sources" in str(app.query_one("#status").render())
        assert not app.query("#model")  # no model picker: this is not a chat client
        await app._refresh_threads()  # a sidebar refresh keeps the full subtitle
        assert app.sub_title == "Deploys · Claude Code · /w/api"


async def test_plain_text_searches_and_open(service: MemoryService) -> None:
    a, _ = await service.import_conversation(
        conversation("a", "kubernetes cluster sizing", title="Alpha", start=1_000)
    )
    await service.import_conversation(conversation("b", "pasta recipe", title="Beta"))
    app = AnamnesisApp(service.settings, service)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: ready(app) and app.thread_id is not None)
        await submit(pilot, app, "kubernetes")
        await wait_until(pilot, lambda: "**Search: kubernetes**" in views(app)[-1].content)
        assert "Alpha" in views(app)[-1].content
        assert len(await service.list_threads()) == 2  # searching never creates anything
        await submit(pilot, app, "/open 1")
        await wait_until(pilot, lambda: app.thread_id == a.id)
        assert views(app)[0].content == "kubernetes cluster sizing"


async def test_history_rename_delete(service: MemoryService) -> None:
    a, _ = await service.import_conversation(
        conversation("a", "first", title="Alpha thread", start=1_000)
    )
    b, _ = await service.import_conversation(conversation("b", "second", title="Beta thread"))
    app = AnamnesisApp(service.settings, service)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: ready(app) and app.thread_id == b.id)  # most recent opened
        await submit(pilot, app, "/history claude-code")
        await wait_until(pilot, lambda: "Alpha thread" in views(app)[-1].content)
        await submit(pilot, app, "/open 2")
        await wait_until(pilot, lambda: app.thread_id == a.id)
        await submit(pilot, app, "/rename Renamed alpha")
        await wait_until(pilot, lambda: app.sub_title.startswith("Renamed alpha"))
        await submit(pilot, app, "/delete")
        await pilot.pause(0.1)
        assert len(await service.list_threads()) == 2  # needs confirmation
        await submit(pilot, app, "/delete")
        await wait_until(pilot, lambda: app.thread_id == b.id)
        assert [t.id for t in await service.list_threads()] == [b.id]
        for gone in ("/new", "/model gemini", "/bogus"):
            await submit(pilot, app, gone)
            await wait_until(pilot, lambda: "Unknown command" in views(app)[-1].content)


async def test_sidebar_selection(service: MemoryService) -> None:
    a, _ = await service.import_conversation(conversation("a", "one", title="First", start=1_000))
    await service.import_conversation(conversation("b", "two", title="Second"))
    app = AnamnesisApp(service.settings, service)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: ready(app) and app.thread_id is not None)
        lv = app.query_one("#threads", ListView)
        lv.focus()
        lv.index = 1
        await pilot.press("enter")
        await wait_until(pilot, lambda: app.thread_id == a.id)


async def test_live_updates_from_background_imports(service: MemoryService) -> None:
    t, _ = await service.import_conversation(conversation("s", "start", title="Shared"))
    app = AnamnesisApp(service.settings, service)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: app.thread_id == t.id)
        # A background sync appends to the open session and adds a new one.
        await service.import_conversation(conversation("s", "start", "continued in Claude Code"))
        await wait_until(
            pilot, lambda: any(v.content == "continued in Claude Code" for v in views(app))
        )
        other, _ = await service.import_conversation(conversation("o", "elsewhere"))
        await wait_until(pilot, lambda: len(list(app.query(ThreadItem))) == 2)
        await service.db.delete_thread(t.id)
        await wait_until(pilot, lambda: app.thread_id is None)
        assert other.id in {i.thread_id for i in app.query(ThreadItem)}


async def test_empty_memory_hint(service: MemoryService) -> None:
    app = AnamnesisApp(service.settings, service)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: ready(app))
        assert "Nothing in memory yet" in str(app.query_one("#empty").render())


async def test_app_creates_and_closes_its_own_service(settings) -> None:  # type: ignore[no-untyped-def]
    """Production path: AnamnesisApp opens the memory itself (semantic off -> no model DL)."""
    s = settings.model_copy(update={"semantic_search": False})
    app = AnamnesisApp(s)
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(pilot, lambda: ready(app))
        assert app.service is not None and app.service.index is None
        assert "keyword search only" in str(app.query_one("#status").render())
    assert app.service.db._conn is None  # closed on exit
    assert s.db_path.exists()
