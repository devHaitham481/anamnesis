"""`anam setup`: the guided first run (and safe to re-run after every upgrade).

1. find the AI tools on this computer,
2. archive their history,
3. offer to stop Claude Code deleting its own sessions (asks first),
4. connect the tools to the memory (asks first), repairing entries left by an older install,
5. show what is archived, how to add web chats, and what to do next.
"""

from __future__ import annotations

import argparse
import contextlib
import sys

from rich.text import Text

from anamnesis.cli.look import AMBER, FAINT, FG, GREEN, MUTED, RED, RUST, console, pill
from anamnesis.config import Settings

DOCS = "https://github.com/devHaitham481/anamnesis#readme"
PAD = "     "  # body text lines up under the step title


def _ask(question: str, assume_yes: bool) -> bool | None:
    """Y/n prompt. None when nobody can answer (no terminal) and --yes wasn't given."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return None
    with contextlib.suppress(ImportError):
        import readline  # noqa: F401  (arrow keys and line editing at the prompt)
    try:
        answer = input(f"{PAD}{question} [Y/n] ").strip().lower()
    except EOFError:
        return None
    return answer in ("", "y", "yes")


def _step(n: int, title: str) -> None:
    con = console()
    con.print()
    con.print(Text.assemble(pill(str(n)), " ", (title, f"bold {FG}")))


def _say(text: str, style: str = MUTED) -> None:
    console().print(Text(f"{PAD}{text}", style=style))


def _row(mark: str, label: str, note: str, color: str = GREEN, note_style: str = MUTED) -> None:
    console().print(
        Text.assemble((f"{PAD}{mark} ", color), (f"{label:<16} ", FG), (note, note_style))
    )


def _cmd(text: str) -> Text:
    return Text(text, style=f"bold {RUST}")


def run(settings: Settings, args: argparse.Namespace) -> int:
    from anamnesis.cli import archive_view
    from anamnesis.cli.__main__ import cmd_sync
    from anamnesis.connect import targets
    from anamnesis.retention import (
        claude_code_cleanup_days,
        deletes_history,
        keep_claude_code_history,
    )

    if hasattr(sys.stdout, "reconfigure"):  # keep step output in order when piped (curl | sh)
        sys.stdout.reconfigure(line_buffering=True)
    con = console()
    con.print()
    con.print(Text.assemble(pill("ANAMNESIS"), ("  setup", f"bold {FG}")))
    con.print(Text(f"  Your archive lives in {settings.data_dir}", style=MUTED))

    _step(1, "AI tools on this computer")
    tools = targets()
    found = [t for t in tools.values() if t.installed()]
    for t in tools.values():
        if t in found:
            _row("✓", t.label, "found")
        else:
            _row("·", t.label, "not installed", color=FAINT, note_style=FAINT)
    if not found:
        _say("None of the supported tools were found. You can still import ChatGPT,")
        _say("claude.ai or Gemini exports later with `anam import <file>`.")

    _step(2, "Archiving your history")
    if args.skip_import:
        _say("skipped (--skip-import)")
    else:
        if not (settings.data_dir / "models").is_dir() and settings.semantic_search:
            _say("First run: downloading the search model (about 70 MB, once).")
        code = cmd_sync(settings, argparse.Namespace(watch=False, force=False, interval=30.0))
        if code != 0:
            print(f"{PAD}Import did not finish; run `anam sync` to try again.", file=sys.stderr)

    _step(3, "Keeping your history")
    days = claude_code_cleanup_days()
    if deletes_history(days):
        _say(f"Claude Code deletes conversations after {days} days. Anamnesis keeps its own")
        _say("copy either way; turning the deletion off keeps them resumable in Claude Code.")
        answer = _ask("Stop Claude Code deleting old conversations?", args.yes)
        if answer is None:
            _say("No terminal to ask in. Run `anam keep` to do it later.", AMBER)
        elif not answer:
            _say("Left as is. Change your mind with `anam keep`.")
        else:
            try:
                _row("✓", "Claude Code", keep_claude_code_history())
            except ValueError as e:
                _row("✗", "Claude Code", str(e), color=RED)
    else:
        _say("Nothing to change: Anamnesis keeps everything it archives, permanently.")

    _step(4, "Connecting your AI tools")
    todo = [t for t in found if not t.current()]
    connected = [t for t in found if t not in todo]
    for t in connected:
        _row("✓", t.label, "already connected")
    failed = False
    if todo:
        names = ", ".join(t.label for t in todo)
        answer = _ask(f"Give {names} access to your memory?", args.yes)
        if answer is None:
            _say("No terminal to ask in. Run `anam connect all` when ready.", AMBER)
        elif not answer:
            _say("Skipped. Connect later with `anam connect all`.")
        else:
            for t in todo:
                try:
                    _row("✓", t.label, t.connect())
                    connected.append(t)
                except Exception as e:
                    _row("✗", t.label, f"failed: {e}", color=RED)
                    failed = True
    elif not found:
        _say("Nothing to connect.")

    archive = archive_view.load(settings)
    con.print()
    con.print(Text.assemble(pill("DONE", GREEN), ("  Your archive:", f"bold {FG}")))
    con.print()
    con.print(archive_view.render_archive(archive))
    missing = archive_view.missing_exports(archive)
    if missing:
        con.print()
        con.print(Text("Add your web chats: export them once.", style=f"bold {FG}"))
        con.print()
        con.print(archive_view.render_exports(missing))

    con.print()
    con.print(pill("NEXT"))
    if connected:
        names = ", ".join(t.label for t in connected)
        _say(f"• Restart {names} (or open a new session) so they load the memory.", FG)
        _say('• Then ask about earlier work, e.g. "what did we decide about auth?"', FG)
    con.print(
        Text.assemble(
            (f"{PAD}• Browse: ", MUTED),
            _cmd("anam"),
            ("   Search: ", MUTED),
            _cmd("anam search <words>"),
            ("   Archive: ", MUTED),
            _cmd("anam status"),
        )
    )
    _say("• New history keeps syncing while a connected tool is running.")
    _say("• claude.ai and ChatGPT (web and phone): run your own node, see `anam node -h`.")
    _say(f"• Docs: {DOCS}")
    con.print()
    return 1 if failed else 0
