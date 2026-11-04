from src.core.context import DEFAULT_SYSTEM_PROMPT, assemble_prompt
from src.core.models import Message, SearchHit


def m(i: int, role: str, text: str) -> Message:
    return Message(id=i, thread_id="t", role=role, content=text, created_at=0)  # type: ignore[arg-type]


def test_basic_shape() -> None:
    out = assemble_prompt([m(1, "user", "hi")])
    assert [x.role for x in out] == ["system", "user"]
    assert out[0].content == DEFAULT_SYSTEM_PROMPT


def test_snippets_injected_into_single_system_message() -> None:
    hit = SearchHit(
        message_id=9,
        thread_id="o",
        role="user",
        content="We use SQLite",
        score=0.9,
        thread_title="Infra",
    )
    out = assemble_prompt([m(1, "user", "db?")], [hit], system_prompt="Custom")
    assert sum(x.role == "system" for x in out) == 1
    assert out[0].content.startswith("Custom")
    assert 'thread "Infra"' in out[0].content and "We use SQLite" in out[0].content


def test_budget_drops_oldest_but_keeps_latest_and_starts_with_user() -> None:
    history = [
        m(1, "user", "a" * 50),
        m(2, "assistant", "b" * 50),
        m(3, "user", "c" * 50),
        m(4, "assistant", "d" * 50),
        m(5, "user", "e" * 500),
    ]
    out = assemble_prompt(history, max_history_chars=620)
    assert [x.content[0] for x in out[1:]] == ["c", "d", "e"]
    out = assemble_prompt(history, max_history_chars=10)
    assert [x.content[0] for x in out[1:]] == ["e"]
    out = assemble_prompt(history, max_history_chars=580)  # would start with assistant "d"
    assert out[1].role == "user"
