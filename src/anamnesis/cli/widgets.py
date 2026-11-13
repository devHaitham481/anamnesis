"""Presentational widgets and the color theme for the TUI. No database access here."""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import PurePath

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.theme import Theme
from textual.widgets import Label, ListItem, Markdown, Static

from anamnesis.core.models import SOURCE_LABELS, Message, Thread

# Warm charcoal with a rust accent: the same palette as the website and logo.
RUST = "#e07a4f"
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

# One color per tool, so where a conversation came from is visible at a glance.
SOURCE_COLORS = {
    "claude-code": "#d97757",
    "claude-web": "#d97757",
    "codex": "#c9c5bd",
    "cursor": "#6fa8dc",
    "gemini-cli": "#8e9cf0",
    "gemini-web": "#8e9cf0",
    "chatgpt": "#5fbf8f",
    "note": "#e0b050",
    "anamnesis": "#8a8378",
}
MUTED = "#8a8378"
YOU = "#c9a26b"  # your own messages: warm sand, distinct from every tool's color

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


def source_color(source: str | None) -> str:
    return SOURCE_COLORS.get(source or "", MUTED)


def relative_time(ts: float, now: float | None = None) -> str:
    """'just now', '5m ago', '3h ago', 'yesterday', '4d ago', then a date."""
    delta = (time.time() if now is None else now) - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 2 * 86400:
        return "yesterday"
    if delta < 7 * 86400:
        return f"{int(delta // 86400)}d ago"
    when = datetime.fromtimestamp(ts)
    this_year = datetime.fromtimestamp(time.time() if now is None else now).year
    return f"{when:%b} {when.day}" + ("" if when.year == this_year else f", {when.year}")


def project_name(project: str | None) -> str:
    return PurePath(project).name if project else ""


def thread_meta(thread: Thread, now: float | None = None) -> str:
    parts = [SOURCE_LABELS.get(thread.source, thread.source), project_name(thread.project)]
    parts.append(relative_time(thread.updated_at, now))
    return " · ".join(p for p in parts if p)


class ThreadItem(ListItem):
    """Two lines: a dot in the tool's color and the title, then tool · project · when."""

    def __init__(self, thread: Thread) -> None:
        title = Text.assemble(("● ", source_color(thread.source)), thread.title or "Untitled")
        super().__init__(
            Label(title, classes="thread-title"),
            Label(thread_meta(thread), classes="thread-meta", markup=False),
        )
        self.thread_id = thread.id
        self.source = thread.source


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
        self.body = Markdown(content)

    def compose(self) -> ComposeResult:
        who = self._header.split(" · ")[0]
        line = Text(who, style=f"bold {self._color or MUTED}")
        if self._when is not None:
            stamp = datetime.fromtimestamp(self._when)
            line.append(f"  {stamp:%b} {stamp.day} · {stamp:%H:%M}", style=MUTED)
        yield Label(line, classes="message-header")
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


def query_terms(query: str) -> list[str]:
    return [w for w in re.findall(r"\w+", query) if len(w) > 2] or query.split()


def snippet(content: str, terms: list[str], width: int = 220) -> str:
    """A window of the message centered on the first matching term, Markdown markers removed."""
    plain = re.sub(r"(\*\*|__|`+)", "", content)
    plain = re.sub(r"(?m)^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+", "", plain)
    text = " ".join(plain.split())
    lower = text.lower()
    hits = [i for i in (lower.find(t.lower()) for t in terms) if i >= 0]
    start = max(0, min(hits) - width // 3) if hits else 0
    if start:
        space = text.find(" ", start)
        start = space + 1 if 0 <= space < start + 20 else start
    out = text[start : start + width]
    if start + width < len(text):
        out = out.rstrip() + "…"
    return ("…" if start else "") + out


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
                ("Search ", f"bold {RUST}"),
                (self._query, "bold"),
                (f"   {n} conversation{'s' if n != 1 else ''} · /open N to read one", MUTED),
            ),
            classes="message-header",
        )
        terms = query_terms(self._query)
        for i, (source, title, meta, text) in enumerate(self._rows, 1):
            with Vertical(classes="result"):
                dot = ("● ", source_color(source))
                yield Label(Text.assemble((f"{i:>2}  ", MUTED), dot, (title, "bold")))
                yield Label(meta, classes="result-meta", markup=False)
                body = Text(snippet(text, terms))
                body.highlight_words(terms, style=f"bold {RUST}", case_sensitive=False)
                yield Static(body, classes="result-snippet")
