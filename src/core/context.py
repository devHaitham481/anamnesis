"""RAG context assembly: system prompt + retrieved past snippets + recent thread history."""

from __future__ import annotations

from collections.abc import Sequence

from src.core.models import Message, SearchHit
from src.providers.models import ChatMessage

DEFAULT_SYSTEM_PROMPT = (
    "You are Anamnesis, a helpful assistant. Answer clearly and concisely; "
    "use Markdown when useful."
)
SNIPPET_CHARS = 600
MAX_HISTORY_CHARS = 60_000


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_snippets(hits: Sequence[SearchHit]) -> str:
    lines = [
        "Relevant excerpts from the user's earlier conversations (may be from other models or "
        "threads). Use them only if they help answer the current message:"
    ]
    for h in hits:
        where = f'thread "{h.thread_title}"' if h.thread_title else "an earlier thread"
        lines.append(f"- [{h.role} in {where}] {_truncate(h.content, SNIPPET_CHARS)}")
    return "\n".join(lines)


def assemble_prompt(
    history: Sequence[Message],
    retrieved: Sequence[SearchHit] = (),
    system_prompt: str | None = None,
    max_history_chars: int = MAX_HISTORY_CHARS,
) -> list[ChatMessage]:
    """Build the provider message list.

    `history` is the thread's recent messages in chronological order, ending with the new user
    message. Older messages are dropped (oldest first) to stay within `max_history_chars`, but the
    latest message is always kept. Retrieved snippets go into the single leading system message so
    the result is valid for both Anthropic and Gemini.
    """
    system = system_prompt or DEFAULT_SYSTEM_PROMPT
    if retrieved:
        system = f"{system}\n\n{format_snippets(retrieved)}"

    kept: list[Message] = []
    budget = max_history_chars
    for msg in reversed([m for m in history if m.role != "system"]):
        if kept and len(msg.content) > budget:
            break
        kept.append(msg)
        budget -= len(msg.content)
    kept.reverse()
    # Providers expect the conversation to start with a user turn.
    while kept and kept[0].role != "user" and len(kept) > 1:
        kept.pop(0)

    return [ChatMessage(role="system", content=system)] + [
        ChatMessage(role=m.role, content=m.content) for m in kept
    ]
