"""Presentational widgets for the TUI. No database access here."""

from __future__ import annotations

from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, ListItem, Markdown

from src.core.models import SOURCE_LABELS, Message, Thread

# Short sidebar badges showing which tool a conversation came from.
SOURCE_TAGS = {
    "anamnesis": "AN",
    "note": "NB",
    "claude-code": "CC",
    "codex": "CX",
    "gemini-cli": "GC",
    "cursor": "CU",
    "chatgpt": "GPT",
    "claude-web": "CL",
    "gemini-web": "GW",
}


class ThreadItem(ListItem):
    def __init__(self, thread: Thread) -> None:
        tag = SOURCE_TAGS.get(thread.source, thread.source[:3].upper())
        super().__init__(Label(f"{tag:<3} {thread.title or 'Untitled'}", markup=False))
        self.thread_id = thread.id
        self.source = thread.source


def message_header(msg: Message, source: str | None = None) -> str:
    when = f"{datetime.fromtimestamp(msg.created_at):%Y-%m-%d %H:%M}"
    if msg.role == "user":
        who = "Note" if source == "note" else "You"
    else:
        who = (msg.model or "").split("/")[-1] or SOURCE_LABELS.get(source or "", "Assistant")
    return f"{who} · {when}"


class MessageView(Vertical):
    """One stored message: a header line plus a Markdown body."""

    def __init__(self, role: str, header: str, content: str = "") -> None:
        super().__init__(classes=f"message {role}")
        self._header = header
        self._content = content
        self.body = Markdown(content)

    def compose(self) -> ComposeResult:
        yield Label(self._header, classes="message-header", markup=False)
        yield self.body

    @classmethod
    def from_message(cls, msg: Message, source: str | None = None) -> MessageView:
        return cls(msg.role, message_header(msg, source), msg.content)

    @property
    def header(self) -> str:
        return self._header

    @property
    def content(self) -> str:
        return self._content
