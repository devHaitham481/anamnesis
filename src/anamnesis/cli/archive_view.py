"""The archive summary shown by `anam status` and at the end of `anam setup`."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime

from anamnesis.config import Settings
from anamnesis.core.models import SOURCE_LABELS
from anamnesis.retention import claude_code_cleanup_days, deletes_history, kept_after_deletion

WEEK = 7 * 86400

# Web chats live on the provider's servers; their data export is the way in.
WEB_EXPORTS = [
    (
        "claude-web",
        "claude.ai (also Claude Desktop and the app)",
        "Settings → Privacy → Export data",
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
        "Google Takeout → only “My Activity” → “Gemini Apps”, format JSON",
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


def print_archive(
    settings: Settings, archive: Archive, *, indent: str = "  ", retention: bool = True
) -> None:
    if not archive.conversations:
        print(f"{indent}Nothing archived yet. Run `anam sync`, or `anam import` an export.")
        return
    since = f", since {_date(archive.first)}" if archive.first else ""
    n, m = archive.conversations, archive.messages
    print(
        f"{indent}{n:,} conversation{'s' if n != 1 else ''}, "
        f"{m:,} message{'s' if m != 1 else ''}{since}"
    )
    print()
    for source, (n, week) in archive.sources.items():
        label = SOURCE_LABELS.get(source, source)
        recent = f"   +{week} this week" if week else ""
        print(f"{indent}{label:<24} {n:>5}{recent}")
    for label, n in archive.kept.items():
        print(
            f"\n{indent}Kept {n} {label} session{'s' if n != 1 else ''} that {label} "
            "has since deleted."
        )
    days = claude_code_cleanup_days()
    if retention and deletes_history(days):
        print(
            f"\n{indent}Claude Code deletes its own copies after {days} days; Anamnesis keeps them."
            f"\n{indent}To keep them resumable in Claude Code too: anam keep"
        )


def print_lookups(archive: Archive, *, indent: str = "  ") -> None:
    n = archive.lookups
    times = "once" if n == 1 else f"{n} times"
    print(f"{indent}Your AI tools looked things up in the memory {times} in the last 7 days.")


def missing_exports(archive: Archive) -> list[tuple[str, str, str, str]]:
    return [e for e in WEB_EXPORTS if e[0] not in archive.sources]


def print_exports(exports: list[tuple[str, str, str, str]], *, indent: str = "  ") -> None:
    for _, name, steps, url in exports:
        print(f"{indent}• {name}: {steps}")
        print(f"{indent}  {url}")
    print(f"{indent}The download arrives by email. Then: anam import ~/Downloads/<file>.zip")
