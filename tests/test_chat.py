import asyncio

import pytest

from src.config import MissingAPIKeyError, Settings
from src.core.chat import INTERRUPTED_NOTE, AssistantSaved, ChatService, Delta, UserSaved
from src.core.db import ThreadNotFoundError
from src.core.vector import HashEmbedder, VectorIndex
from src.providers.router import ProviderRouter, UnknownModelError
from tests.conftest import FakeLLM


async def run(svc: ChatService, thread_id: str, text: str, model: str | None = None) -> list:
    return [e async for e in svc.send(thread_id, text, model)]


async def test_turn_persists_streams_titles_and_indexes(service: ChatService, fake_llm: FakeLLM):
    t = await service.new_thread()
    events = await run(service, t.id, "  Plan the database layer please  ")
    assert isinstance(events[0], UserSaved) and isinstance(events[-1], AssistantSaved)
    assert "".join(e.text for e in events if isinstance(e, Delta)) == fake_llm.reply
    msgs = await service.messages(t.id)
    assert [(x.role, x.content) for x in msgs] == [
        ("user", "Plan the database layer please"),
        ("assistant", fake_llm.reply),
    ]
    assert msgs[1].provider == "anthropic" and msgs[1].model == service.settings.claude_model
    assert (await service.get_thread(t.id)).title == "Plan the database layer please"
    assert service.index is not None and service.index.count() == 2


async def test_rag_injects_cross_thread_context(service: ChatService, fake_llm: FakeLLM):
    old = await service.new_thread()
    await run(service, old.id, "Our deployment target is a Raspberry Pi cluster in the garage")
    new = await service.new_thread(model="gemini")
    await run(service, new.id, "Remind me about the deployment target raspberry cluster")
    system = fake_llm.last_messages[0]
    assert system["role"] == "system"
    assert "Raspberry Pi cluster in the garage" in system["content"]
    assert fake_llm.calls[-1]["model"] == service.settings.gemini_model
    # The current message itself is not echoed back as a snippet.
    assert "Remind me" not in system["content"]


async def test_history_is_sent(service: ChatService, fake_llm: FakeLLM):
    t = await service.new_thread()
    await run(service, t.id, "first")
    await run(service, t.id, "second")
    roles = [m["role"] for m in fake_llm.last_messages]
    assert roles == ["system", "user", "assistant", "user"]


async def test_model_switch_mid_thread(service: ChatService, fake_llm: FakeLLM):
    t = await service.new_thread(model="claude")
    await run(service, t.id, "hello")
    await run(service, t.id, "hello again", model="gemini")
    assert (await service.get_thread(t.id)).model == "gemini"
    assert [m.provider for m in await service.messages(t.id) if m.role == "assistant"] == [
        "anthropic",
        "gemini",
    ]


async def test_validation_errors_persist_nothing(service: ChatService, settings: Settings):
    t = await service.new_thread()
    with pytest.raises(ValueError):
        await run(service, t.id, "   ")
    with pytest.raises(UnknownModelError):
        await run(service, t.id, "hi", model="gpt-4")
    with pytest.raises(ThreadNotFoundError):
        await run(service, "missing", "hi")
    no_keys = ChatService(service.db, ProviderRouter(Settings()), None, Settings())
    with pytest.raises(MissingAPIKeyError):
        await run(no_keys, t.id, "hi")
    assert await service.messages(t.id) == []


async def test_provider_error_keeps_user_message(service: ChatService, fake_llm: FakeLLM):
    t = await service.new_thread()
    fake_llm.fail = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await run(service, t.id, "hi")
    assert [m.role for m in await service.messages(t.id)] == ["user"]


async def test_interrupted_stream_saves_partial(service: ChatService, fake_llm: FakeLLM):
    t = await service.new_thread()
    fake_llm.reply = "word " * 50
    gen = service.send(t.id, "go")
    async for e in gen:
        if isinstance(e, Delta):
            break
    await gen.aclose()
    msgs = await service.messages(t.id)
    assert msgs[-1].role == "assistant" and msgs[-1].content.endswith(INTERRUPTED_NOTE)


async def test_search_merges_semantic_and_keyword(service: ChatService):
    t = await service.new_thread()
    await run(service, t.id, "kubernetes helm chart upgrade")
    hits = await service.search("helm chart")
    assert hits and hits[0].thread_title and "helm" in hits[0].content
    await service.delete_thread(t.id)
    assert await service.search("helm chart") == []
    assert service.index is not None and service.index.count() == 0


async def test_backfill_indexes_unindexed_messages(service: ChatService, settings: Settings):
    t = await service.new_thread()
    await service.db.add_message(t.id, "user", "written while index was offline")
    fresh = VectorIndex(settings.data_dir / "other", HashEmbedder())
    svc2 = ChatService(service.db, service.router, fresh, settings)
    assert await svc2.backfill_index() == 1
    assert await svc2.backfill_index() == 0


async def test_create_degrades_when_embedder_unavailable(settings: Settings, fake_llm: FakeLLM):
    bad = settings.model_copy(update={"embedding_model": "not/a-real-model"})
    svc = await ChatService.create(bad, ProviderRouter(bad, completion_fn=fake_llm))
    try:
        assert svc.index is None
        t = await svc.new_thread()
        await run(svc, t.id, "still works")
        assert len(await svc.messages(t.id)) == 2
        assert await svc.db.list_providers() == {
            "claude": bad.claude_model,
            "gemini": bad.gemini_model,
        }
    finally:
        await svc.close()


async def test_concurrent_turns_in_different_threads(service: ChatService):
    a, b = await service.new_thread(), await service.new_thread()
    await asyncio.gather(run(service, a.id, "alpha"), run(service, b.id, "beta"))
    assert len(await service.messages(a.id)) == 2 and len(await service.messages(b.id)) == 2
