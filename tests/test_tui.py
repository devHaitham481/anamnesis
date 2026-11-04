"""Headless TUI tests via Textual's pilot, against the real ChatService + fake LLM."""

import asyncio

from textual.widgets import Input, ListView, Select

from src.cli.app import NexusApp
from src.cli.widgets import MessageView, ThreadItem
from src.core.chat import ChatService
from tests.conftest import FakeLLM


async def wait_until(pilot, cond, limit: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + limit
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await pilot.pause(0.05)


def views(app: NexusApp) -> list[MessageView]:
    return list(app.query(MessageView))


async def submit(pilot, app: NexusApp, text: str) -> None:  # type: ignore[no-untyped-def]
    prompt = app.query_one("#prompt", Input)
    prompt.value = text
    await pilot.press("enter")


async def test_chat_flow_streams_and_persists(service: ChatService, fake_llm: FakeLLM) -> None:
    app = NexusApp(service.settings, service)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: not app.query_one("#prompt", Input).disabled)
        await submit(pilot, app, "Hello there NexusAI")
        await wait_until(pilot, lambda: len(views(app)) == 2 and app._streaming is None)
        assert views(app)[0].content == "Hello there NexusAI"
        assert views(app)[1].content == fake_llm.reply
        await wait_until(pilot, lambda: views(app)[1].rendered == fake_llm.reply)
        assert views(app)[0].rendered == "Hello there NexusAI"
        assert app.thread_id is not None
        msgs = await service.messages(app.thread_id)
        assert [m.role for m in msgs] == ["user", "assistant"]
        items = list(app.query(ThreadItem))
        assert len(items) == 1 and items[0].has_class("current")
        assert app.sub_title == "Hello there NexusAI"


async def test_long_stream_renders_progressively(service: ChatService, fake_llm: FakeLLM) -> None:
    fake_llm.reply = (
        "## Title\n\n" + " ".join(f"word{i}" for i in range(300)) + "\n\n```py\nx = 1\n```"
    )
    app = NexusApp(service.settings, service)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: not app.query_one("#prompt", Input).disabled)
        await submit(pilot, app, "write a lot")
        await wait_until(pilot, lambda: app._streaming is None and len(views(app)) == 2)
        await wait_until(pilot, lambda: views(app)[1].rendered == fake_llm.reply)
        assert "streaming" not in str(views(app)[1].query_one(".message-header").render())


async def test_model_toggle_and_command(service: ChatService, fake_llm: FakeLLM) -> None:
    app = NexusApp(service.settings, service)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: not app.query_one("#prompt", Input).disabled)
        assert app.model == "claude"
        await pilot.press("ctrl+t")
        await wait_until(pilot, lambda: app.model == "gemini")
        assert app.query_one("#model", Select).value == "gemini"
        await submit(pilot, app, "/model claude")
        await wait_until(pilot, lambda: app.model == "claude")
        await submit(pilot, app, "/model gemini")
        await wait_until(pilot, lambda: app.model == "gemini")
        await submit(pilot, app, "hi")
        await wait_until(pilot, lambda: app._streaming is None and len(views(app)) >= 4)
        assert fake_llm.calls[-1]["model"] == service.settings.gemini_model
        assert app.thread_id and (await service.get_thread(app.thread_id)).model == "gemini"


async def test_history_search_open_rename_delete(service: ChatService) -> None:
    a = await service.new_thread(title="Alpha thread")
    await service.db.add_message(a.id, "user", "kubernetes cluster sizing")
    b = await service.new_thread(title="Beta thread")
    await service.db.add_message(b.id, "user", "pasta recipe")
    app = NexusApp(service.settings, service)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: app.thread_id == b.id)  # most recent opened
        await submit(pilot, app, "/history")
        await wait_until(pilot, lambda: "Alpha thread" in views(app)[-1].content)
        await submit(pilot, app, "/search kubernetes")
        await wait_until(pilot, lambda: "Alpha thread" in views(app)[-1].content)
        await submit(pilot, app, "/open 1")
        await wait_until(pilot, lambda: app.thread_id == a.id)
        assert views(app)[0].content == "kubernetes cluster sizing"
        await submit(pilot, app, "/rename Renamed alpha")
        await wait_until(pilot, lambda: app.sub_title == "Renamed alpha")
        await submit(pilot, app, "/delete")
        await pilot.pause(0.1)
        assert len(await service.list_threads()) == 2  # needs confirmation
        await submit(pilot, app, "/delete")
        await wait_until(pilot, lambda: app.thread_id == b.id)
        assert [t.id for t in await service.list_threads()] == [b.id]
        await submit(pilot, app, "/bogus")
        await wait_until(pilot, lambda: "Unknown command" in views(app)[-1].content)


async def test_sidebar_selection_and_new_thread(service: ChatService) -> None:
    a = await service.new_thread(title="First")
    await service.new_thread(title="Second")
    app = NexusApp(service.settings, service)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: app.thread_id is not None)
        lv = app.query_one("#threads", ListView)
        lv.focus()
        lv.index = 1
        await pilot.press("enter")
        await wait_until(pilot, lambda: app.thread_id == a.id)
        await pilot.press("ctrl+n")
        await wait_until(pilot, lambda: len(list(app.query(ThreadItem))) == 3)
        assert app.thread_id not in {a.id}


async def test_live_sync_from_other_client(service: ChatService) -> None:
    t = await service.new_thread(title="Shared")
    app = NexusApp(service.settings, service)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: app.thread_id == t.id)
        # Simulate the web UI writing to the same database.
        await service.db.add_message(t.id, "user", "sent from the browser")
        await wait_until(
            pilot, lambda: any(v.content == "sent from the browser" for v in views(app))
        )
        other = await service.db.create_thread("Created elsewhere")
        await wait_until(pilot, lambda: len(list(app.query(ThreadItem))) == 2)
        await service.db.delete_thread(t.id)
        await wait_until(pilot, lambda: app.thread_id is None)
        assert other.id in {i.thread_id for i in app.query(ThreadItem)}


async def test_missing_key_blocks_send(service: ChatService, fake_llm: FakeLLM) -> None:
    from src.config import Settings
    from src.providers.router import ProviderRouter

    s = service.settings.model_copy(update={"anthropic_api_key": None})
    svc = ChatService(service.db, ProviderRouter(s, completion_fn=fake_llm), None, s)
    app = NexusApp(s, svc)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: not app.query_one("#prompt", Input).disabled)
        await submit(pilot, app, "hello")
        await wait_until(pilot, lambda: any("ANTHROPIC_API_KEY" in v.content for v in views(app)))
        assert fake_llm.calls == []
        assert app.query_one("#prompt", Input).value == "hello"  # input preserved
    assert isinstance(s, Settings)


async def test_provider_error_is_shown(service: ChatService, fake_llm: FakeLLM) -> None:
    fake_llm.fail = RuntimeError("rate limited")
    app = NexusApp(service.settings, service)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: not app.query_one("#prompt", Input).disabled)
        await submit(pilot, app, "hello")
        await wait_until(pilot, lambda: any("rate limited" in v.content for v in views(app)))
        assert app._streaming is None
