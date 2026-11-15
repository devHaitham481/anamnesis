"""Presentational widgets and the color theme for the TUI. No database access here."""

from __future__ import annotations

from datetime import datetime

from rich.console import Group
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from rich.theme import Theme as RichTheme
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.theme import Theme
from textual.widgets import Label, ListItem, Static

from anamnesis.cli.look import (
    MUTED,
    RUST,
    SAND,
    pill,
    project_name,
    query_terms,
    relative_time,
    snippet,
    source_color,
)
from anamnesis.core.models import SOURCE_LABELS, Message, Thread

YOU = SAND  # your own messages: warm sand, distinct from every tool's color

THEME = Theme(
    name="anamnesis",
    primary=RUST,
    secondary="#c9a26b",
    accent=RUST,
    foreground="#ece8e1",
    background="#121110",
    surface="#171614",
    panel="#221f1c",
    success="#5fbf8f",
    warning="#e0b050",
    error="#e5534b",
    dark=True,
    variables={
        "border": RUST,
        "border-blurred": "#2f2b27",
        "text-muted": "#8a8378",
        "footer-key-foreground": RUST,
        "footer-background": "#121110",
        "footer-item-background": "#121110",
        "input-selection-background": f"{RUST} 35%",
        "scrollbar": "#2f2b27",
        "scrollbar-hover": "#4a443d",
        "scrollbar-active": RUST,
        "scrollbar-background": "#121110",
        "scrollbar-background-hover": "#121110",
        "scrollbar-background-active": "#121110",
    },
)

# Rich draws message Markdown; its defaults (bright cyan code, etc.) clash with the palette.
MARKDOWN_STYLES = RichTheme(
    {
        "markdown.code": "#e8c08a on #221f1c",
        "markdown.h1": f"bold {RUST}",
        "markdown.h2": f"bold {RUST}",
        "markdown.h3": "bold #ece8e1",
        "markdown.h4": "bold #ece8e1",
        "markdown.link": "underline #c9a26b",
        "markdown.link_url": "#8a8378",
        "markdown.block_quote": "#b5ada1",
        "markdown.item.bullet": RUST,
        "markdown.item.number": RUST,
        "markdown.hr": "#2f2b27",
    }
)

# Short badges, used in /history listings and kept for backwards compatibility.
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


def thread_meta(thread: Thread, now: float | None = None) -> str:
    parts = [SOURCE_LABELS.get(thread.source, thread.source), project_name(thread.project)]
    parts.append(relative_time(thread.updated_at, now))
    return " · ".join(p for p in parts if p)


class ThreadItem(ListItem):
    """Two lines: a dot in the tool's color and the title, then tool · project · when.

    Rendered directly (no child widgets): Textual restyles a focused widget's whole subtree,
    so fewer widgets per row keeps moving focus to the list fast.
    """

    def __init__(self, thread: Thread) -> None:
        super().__init__()
        self.thread_id = thread.id
        self.source = thread.source
        self._title = thread.title or "Untitled"
        self._meta = thread_meta(thread)

    def update(self, thread: Thread) -> None:
        title, meta = thread.title or "Untitled", thread_meta(thread)
        if (title, meta) != (self._title, self._meta):
            self._title, self._meta = title, meta
            self.refresh()

    def render(self) -> Text:
        width = max(self.content_size.width, 1)
        title = Text.assemble(
            ("● ", source_color(self.source)),
            (self._title, "bold" if self.has_class("current") else ""),
        )
        meta = Text(f"  {self._meta}", style=MUTED)
        for line in (title, meta):
            line.truncate(width, overflow="ellipsis")
        return Text("\n").join([title, meta])


def message_header(msg: Message, source: str | None = None) -> str:
    when = f"{datetime.fromtimestamp(msg.created_at):%Y-%m-%d %H:%M}"
    return f"{speaker(msg, source)} · {when}"


def speaker(msg: Message, source: str | None = None) -> str:
    if msg.role == "user":
        return "Note" if source == "note" else "You"
    return (msg.model or "").split("/")[-1] or SOURCE_LABELS.get(source or "", "Assistant")


class MessageView(Vertical):
    """One stored message: a colored bar, the speaker and time, then a Markdown body."""

    def __init__(
        self,
        role: str,
        header: str,
        content: str = "",
        *,
        color: str | None = None,
        when: float | None = None,
    ) -> None:
        super().__init__(classes=f"message {role}")
        self._header = header
        self._content = content
        self._color = color
        self._when = when
        # Header and body in one Static, with Rich rendering the Markdown: Textual's Markdown
        # widget builds a widget per block, which made large conversations slow to open and
        # every focus change slow to restyle.
        who = header.split(" · ")[0]
        line = Text(who, style=f"bold {color or MUTED}")
        if when is not None:
            stamp = datetime.fromtimestamp(when)
            line.append(f"  {stamp:%b} {stamp.day} · {stamp:%H:%M}", style=MUTED)
        markdown = RichMarkdown(content, code_theme="monokai", inline_code_theme="monokai")
        self.body = Static(Group(line, markdown), classes="body")

    def compose(self) -> ComposeResult:
        yield self.body

    def on_mount(self) -> None:
        if self._color:
            self.styles.border_left = ("outer", self._color)

    @classmethod
    def from_message(cls, msg: Message, source: str | None = None) -> MessageView:
        color = YOU if msg.role == "user" and source != "note" else source_color(source)
        return cls(
            msg.role, message_header(msg, source), msg.content, color=color, when=msg.created_at
        )

    @property
    def header(self) -> str:
        return self._header

    @property
    def content(self) -> str:
        return self._content


class SearchResultsView(MessageView):
    """Search results as cards: tool dot + title, tool · project · when, matching snippet.

    `content` keeps a Markdown rendering of the same results (used for copy/tests).
    """

    def __init__(self, query: str, rows: list[tuple[str, str, str, str]], content: str) -> None:
        super().__init__("note search", f"Search · {query}", content)
        self._query = query
        self._rows = rows  # (source, title, meta, text)

    def compose(self) -> ComposeResult:
        n = len(self._rows)
        yield Label(
            Text.assemble(
                pill("SEARCH"),
                ("  ", ""),
                (self._query, "bold"),
                (f"   {n} conversation{'s' if n != 1 else ''} · /open N to read one", MUTED),
            ),
            classes="message-header",
        )
        terms = query_terms(self._query)
        for i, (source, title, meta, text) in enumerate(self._rows, 1):
            with Vertical(classes="result"):
                yield Label(Text.assemble((f"{i:>2}  ", MUTED), (title, "bold")))
                tool, _, rest = meta.partition(" · ")
                yield Label(
                    Text.assemble(pill(tool, source_color(source)), (f"  {rest}", MUTED)),
                    classes="result-meta",
                )
                body = Text(snippet(text, terms))
                body.highlight_words(terms, style=f"bold {RUST}", case_sensitive=False)
                yield Static(body, classes="result-snippet")
