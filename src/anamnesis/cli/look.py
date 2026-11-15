"""The terminal look, shared by the CLI and the TUI: palette, pills, boxes and bars.

Styled after Charm's Lip Gloss: rounded borders, generous padding, small colored "pill"
labels, and quiet secondary text, in Anamnesis's warm charcoal and rust. Pure Rich, no
Textual import, so the plain commands stay fast.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from pathlib import PurePath

from rich import box
from rich.console import Console, Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

# ---------- palette (same as the website and logo) ----------

BG = "#121110"
FG = "#ece8e1"
RUST = "#e07a4f"
SAND = "#c9a26b"  # your own messages
MUTED = "#8a8378"
FAINT = "#4a443d"
BLURRED = "#2f2b27"
GREEN = "#5fbf8f"
AMBER = "#e0b050"
RED = "#e5534b"

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


def source_color(source: str | None) -> str:
    return SOURCE_COLORS.get(source or "", MUTED)


# ---------- building blocks ----------


def pill(label: str, color: str = RUST, fg: str = BG) -> Text:
    """A small solid label: ` ARCHIVE `."""
    # A styled span, not a base style, so text appended after the pill stays unstyled.
    return Text.assemble((f" {label} ", f"bold {fg} on {color}"))


def heading(label: str, note: str = "", color: str = RUST) -> Text:
    """A pill followed by a muted note, used to start each section."""
    text = pill(label, color)
    if note:
        text.append(f"  {note}", style=MUTED)
    return text


def card(*rows: RenderableType, width: int | None = None, color: str = FAINT) -> Panel:
    """Rounded box with Lip Gloss-style padding."""
    return Panel(
        Group(*rows),
        box=box.ROUNDED,
        border_style=color,
        padding=(1, 3),
        width=width,
        expand=False,
    )


def bar(value: int, top: int, width: int = 18, color: str = RUST) -> Text:
    """A thin horizontal bar: filled part in `color`, the rest faint."""
    filled = 0 if top <= 0 else max(1 if value else 0, round(width * value / top))
    return Text.assemble(("━" * filled, color), ("━" * (width - filled), BLURRED))


def dot(source: str | None) -> Text:
    return Text("● ", style=source_color(source))


def indent(renderable: RenderableType, left: int = 2) -> Padding:
    return Padding(renderable, (0, 0, 0, left))


def console() -> Console:
    """A console on the current stdout (so redirects and tests see the output).

    Never wraps plain lines mid-word; colors only on a real terminal.
    """
    return Console(file=sys.stdout, highlight=False, soft_wrap=True)


def box_width(con: Console, most: int = 78) -> int:
    return max(48, min(con.width, most))


# ---------- text helpers (also used by the TUI) ----------


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


def fuzzy_score(query: str, text: str) -> float | None:
    """Fuzzy match like fzf: every query character in order. None = no match.

    Several words must each match. Consecutive characters and matches at word starts score
    higher, so "sess mid" ranks "Add session middleware" above "session ... mid-term".
    """
    hay = text.lower()
    total = 0.0
    for word in query.lower().split():
        pos, score, prev = 0, 0.0, -2
        for ch in word:
            found = hay.find(ch, pos)
            if found < 0:
                return None
            score += 1.0
            if found == prev + 1:
                score += 2.0  # consecutive
            if found == 0 or not hay[found - 1].isalnum():
                score += 3.0  # start of a word
            prev, pos = found, found + 1
        if word in hay:
            score += 5.0 * len(word)  # the plain substring beats a scattered match
        total += score
    return total
