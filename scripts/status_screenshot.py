"""Render docs/status.svg: `anam status`, then an agent answering from memory (sample data).

The archive card and tool list are drawn by the same code `anam status` runs; the numbers and
the conversation are made up, so no real history ends up in the repo.
    python scripts/status_screenshot.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rich import box  # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich.padding import Padding  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.terminal_theme import TerminalTheme  # noqa: E402
from rich.text import Text  # noqa: E402

from anamnesis.cli import archive_view  # noqa: E402
from anamnesis.cli.look import (  # noqa: E402
    BG,
    FG,
    MUTED,
    RUST,
    SAND,
    pill,
    source_color,
)

DAY = 86_400.0

ARCHIVE = archive_view.Archive(
    sources={
        "claude-code": (148, 23),
        "chatgpt": (412, 6),
        "claude-web": (187, 4),
        "codex": (36, 5),
        "cursor": (21, 0),
    },
    first=time.time() - 641 * DAY,
    messages=48_213,
    kept={"Claude Code": 91},
    lookups=27,
)
TOOLS = [
    ("Claude Code", "connected"),
    ("Claude Desktop", "connected"),
    ("Codex CLI", "connected"),
    ("Cursor", "connected"),
]

THEME = TerminalTheme(
    (0x12, 0x11, 0x10),
    (0xEC, 0xE8, 0xE1),
    [
        (0x12, 0x11, 0x10),
        (0xE5, 0x53, 0x4B),
        (0x5F, 0xBF, 0x8F),
        (0xE0, 0xB0, 0x50),
        (0x6F, 0xA8, 0xDC),
        (0xD9, 0x77, 0x57),
        (0x8E, 0x9C, 0xF0),
        (0xEC, 0xE8, 0xE1),
    ],
)


def prompt(cmd: str) -> Text:
    return Text.assemble(("❯ ", RUST), (cmd, f"bold {FG}"))


def recall() -> Group:
    ask = Text.assemble(
        ("> ", MUTED), ("why did we pick session cookies over JWT?", f"bold {SAND}")
    )
    call = Text.assemble(
        ("● ", RUST),
        ("anamnesis", f"bold {FG}"),
        (" · search_memory", MUTED),
        ('("session cookies vs JWT")', MUTED),
    )
    hit = Panel(
        Group(
            Text.assemble(
                pill("ChatGPT", source_color("chatgpt")), ("  api-server · Mar 4", MUTED)
            ),
            Text(""),
            Text(
                "“We need instant revocation, so server-side sessions;\n"
                " JWT would mean a denylist anyway.”",
                style=FG,
            ),
        ),
        box=box.ROUNDED,
        border_style=source_color("chatgpt"),
        padding=(0, 2),
        expand=False,
    )
    answer = Text(
        "You decided this in ChatGPT on March 4: removed users must lose\n"
        "access at once, and JWTs would have needed a denylist anyway.",
        style=FG,
    )
    return Group(ask, Text(""), call, Padding(hit, (1, 0, 1, 2)), answer)


def main() -> None:
    con = Console(
        record=True,
        width=84,
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
        file=open("/dev/null", "w"),
    )  # noqa: SIM115
    con.print(prompt("anam status"))
    con.print()
    con.print(archive_view.render_archive(ARCHIVE, retention_days=30))
    con.print()
    con.print(archive_view.render_lookups(ARCHIVE))
    con.print()
    con.print(archive_view.render_tools(TOOLS))
    con.print()
    con.print(prompt("claude"))
    con.print()
    con.print(Padding(recall(), (0, 0, 0, 2)))
    out = ROOT / "docs" / "status.svg"
    con.save_svg(str(out), title="anam status", theme=THEME)
    svg = out.read_text().replace(f'fill="#{0x12:02x}{0x11:02x}{0x10:02x}"', f'fill="{BG}"')
    out.write_text(svg)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
