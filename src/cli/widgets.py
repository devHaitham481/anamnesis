"""Presentational widgets for the TUI. No database or provider access here."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, ListItem, Markdown
from textual.widgets.markdown import MarkdownStream

from src.core.models import Message, Thread


class ThreadItem(ListItem):
    def __init__(self, thread: Thread) -> None:
        super().__init__(Label(thread.title or "Untitled", markup=False))
        self.thread_id = thread.id


class MessageView(Vertical):
    """One chat bubble: a header line plus a Markdown body."""

    def __init__(self, role: str, header: str, content: str = "") -> None:
        super().__init__(classes=f"message {role}")
        self._header = header
        self._content = content
        self.body = Markdown(content)
        self._stream: MarkdownStream | None = None

    def compose(self) -> ComposeResult:
        yield Label(self._header, classes="message-header", markup=False)
        yield self.body

    @classmethod
    def from_message(cls, msg: Message) -> MessageView:
        if msg.role == "user":
            header = "You"
        else:
            model = (msg.model or "assistant").split("/")[-1]
            header = f"{model} · {msg.provider}" if msg.provider else model
        return cls(msg.role, header, msg.content)

    def set_header(self, header: str) -> None:
        self.query_one(".message-header", Label).update(header)

    async def write(self, text: str) -> None:
        if self._stream is None:
            self._stream = Markdown.get_stream(self.body)
        self._content += text
        await self._stream.write(text)

    async def finish(self) -> None:
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None

    @property
    def content(self) -> str:
        return self._content

    @property
    def rendered(self) -> str:
        """Markdown source currently shown on screen."""
        return self.body.source
