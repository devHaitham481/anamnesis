"""Slash-command parsing for the TUI (pure, UI-independent)."""

from __future__ import annotations

from dataclasses import dataclass

COMMANDS: dict[str, str] = {
    "help": "Show this help",
    "search": "/search <query> — search all memory (or just type the query); open with /open N",
    "history": "/history [source] — recent conversations, optionally from one tool "
    "(claude-code, codex, gemini-cli, cursor, chatgpt, claude-web, gemini-web, note)",
    "open": "/open N — open item N from the last /history or /search listing",
    "rename": "/rename <title> — rename the open conversation",
    "delete": "/delete — delete the open conversation from memory (run twice to confirm)",
    "sync": "/sync — import new history from your other AI tools now",
    "quit": "Exit Anamnesis",
}
_REQUIRES_ARG = {"search", "open", "rename"}


class CommandError(ValueError):
    pass


@dataclass(frozen=True)
class Command:
    name: str
    arg: str = ""


def is_command(text: str) -> bool:
    return text.lstrip().startswith("/")


def parse_command(text: str) -> Command:
    body = text.strip()[1:]
    name, _, arg = body.partition(" ")
    name, arg = name.lower(), arg.strip()
    if name in {"exit", "q"}:
        name = "quit"
    if name not in COMMANDS:
        raise CommandError(f"Unknown command /{name}. Type /help for the list.")
    if name in _REQUIRES_ARG and not arg:
        raise CommandError(f"Usage: {COMMANDS[name]}")
    if name == "open" and not arg.isdigit():
        raise CommandError("Usage: /open N (a number from the last listing)")
    return Command(name, arg)


def help_text() -> str:
    rows = "\n".join(f"- `/{n}` — {d.split(' — ')[-1]}" for n, d in COMMANDS.items())
    keys = "**Keys:** `Ctrl+L` conversation list · `Ctrl+F` / `Esc` search box · `Ctrl+Q` quit"
    return f"**Commands**\n\n{rows}\n\n{keys}"
