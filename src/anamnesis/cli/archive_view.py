"""The archive summary: `anam status`, and the end of `anam setup`.

`load()` reads the numbers; `render_*()` returns Rich renderables in the shared look, so the
README screenshot is drawn by exactly the code users run.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from anamnesis.cli.look import (
    AMBER,
    FAINT,
    FG,
    GREEN,
    MUTED,
    RUST,
    bar,
    card,
    dot,
    heading,
    indent,
    source_color,
)
from anamnesis.config import Settings
from anamnesis.core.models import SOURCE_LABELS
from anamnesis.retention import deletes_history, kept_after_deletion

WEEK = 7 * 86400

# Web chats live on the provider's servers; their data export is the way in.
WEB_EXPORTS = [
    (
        "claude-web",
        "claude.ai",
        "Settings → Privacy → Export data  (incl. Desktop & app)",
        "https://claude.ai/settings/data-privacy-controls",
    ),
    (
        "chatgpt",
        "ChatGPT",
        "Settings → Data controls → Export data",
        "https://chatgpt.com/#settings/DataControls",
    ),
    (
        "gemini-web",
        "Gemini",
        "Takeout → only “My Activity” → “Gemini Apps”, JSON",
        "https://takeout.google.com",
    ),
]


@dataclass
class Archive:
    sources: dict[str, tuple[int, int]] = field(default_factory=dict)  # source -> (total, week)
    first: float | None = None
    messages: int = 0
    kept: dict[str, int] = field(default_factory=dict)  # tool label -> sessions it deleted
    lookups: int = 0

    @property
    def conversations(self) -> int:
        return sum(n for n, _ in self.sources.values())


def load(settings: Settings) -> Archive:
    from anamnesis.core.db import Database

    async def run() -> Archive:
        since = time.time() - WEEK
        async with Database(settings.db_path) as db:
            summary = await db.archive_summary(since)
            ingested = await db.ingested_paths()
            lookups = await db.lookup_count(since)
        return Archive(
            sources=summary["sources"],
            first=summary["first"],
            messages=summary["messages"],
            kept=kept_after_deletion(ingested),
            lookups=lookups,
        )

    return asyncio.run(run())


def _date(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    return f"{d:%b} {d.day}, {d.year}"


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def render_archive(archive: Archive, *, retention_days: int | None = None) -> RenderableType:
    """The headline card: totals, one bar per tool, what was kept, Claude Code's deletion."""
    if not archive.conversations:
        return card(
            heading("ARCHIVE", "nothing archived yet"),
            Text(""),
            Text("Run `anam sync`, or `anam import` an export.", style=MUTED),
        )
    totals = Text.assemble(
        (f"{archive.conversations:,}", f"bold {RUST}"),
        (" conversations   ", MUTED),
        (f"{archive.messages:,}", f"bold {FG}"),
        (" messages", MUTED),
    )
    if archive.first:
        totals.append(f"   since {_date(archive.first)}", style=MUTED)

    table = Table.grid(padding=(0, 2))
    for justify in ("left", "right", "left", "left"):
        table.add_column(justify=justify)
    top = max(n for n, _ in archive.sources.values())
    ranked = sorted(archive.sources.items(), key=lambda kv: -kv[1][0])
    for source, (n, week) in ranked:
        table.add_row(
            Text.assemble(dot(source), (SOURCE_LABELS.get(source, source), FG)),
            Text(f"{n:,}", style="bold"),
            bar(n, top, color=source_color(source)),
            Text(f"+{week} this week", style=GREEN) if week else Text(""),
        )

    rows: list[RenderableType] = [heading("ARCHIVE", "every conversation, kept"), Text(""), totals]
    rows += [Text(""), table]
    notes: list[Text] = []
    for label, n in archive.kept.items():
        notes.append(
            Text.assemble(
                ("✓ ", GREEN), (f"Kept {_plural(n, 'session')} that {label} has since deleted", FG)
            )
        )
    if deletes_history(retention_days):
        notes.append(
            Text.assemble(
                ("! ", AMBER),
                (f"Claude Code deletes its own copies after {retention_days} days. ", MUTED),
                ("anam keep", f"bold {RUST}"),
                (" stops that.", MUTED),
            )
        )
    if notes:
        rows += [Text(""), *notes]
    return card(*rows)


def render_lookups(archive: Archive) -> Text:
    n = archive.lookups
    if n == 0:
        note = "no lookups in the last 7 days yet"
    else:
        note = f"looked things up {'once' if n == 1 else f'{n} times'} in the last 7 days"
    return heading("AGENTS", note, color=GREEN if n else FAINT)


TOOL_STATES = {
    "connected": ("●", GREEN, ""),
    "outdated": ("●", AMBER, "anam connect all"),
    "not connected": ("○", MUTED, "anam connect all"),
    "not installed": ("·", FAINT, ""),
}


def render_tools(tools: list[tuple[str, str]]) -> RenderableType:
    """(label, state) per AI tool: connected / outdated / not connected / not installed."""
    table = Table.grid(padding=(0, 2))
    for _ in range(3):
        table.add_column()
    for label, state in tools:
        mark, color, hint = TOOL_STATES.get(state, ("·", MUTED, ""))
        table.add_row(
            Text.assemble((f"{mark} ", color), (label, FAINT if state == "not installed" else FG)),
            Text(state, style=color),
            Text(hint, style=f"bold {RUST}"),
        )
    return Group(heading("TOOLS", "connected over MCP"), Text(""), indent(table))


def missing_exports(archive: Archive) -> list[tuple[str, str, str, str]]:
    return [e for e in WEB_EXPORTS if e[0] not in archive.sources]


def render_exports(exports: list[tuple[str, str, str, str]]) -> RenderableType:
    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True)
    table.add_column()
    for source, name, steps, url in exports:
        table.add_row(
            Text.assemble(dot(source), (name, f"bold {FG}")),
            Text.assemble((steps, MUTED), "\n", (url, f"underline {MUTED}")),
        )
    tail = Text.assemble(
        ("The link arrives by email. Then ", MUTED),
        ("anam import ~/Downloads/<file>.zip", f"bold {RUST}"),
    )
    return Group(
        heading("WEB CHATS", "not imported yet: they live on the providers' servers"),
        Text(""),
        indent(table),
        Text(""),
        indent(tail),
    )
