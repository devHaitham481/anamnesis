"""`anam setup`: the guided first run (and safe to re-run after every upgrade).

1. find the AI tools on this computer,
2. archive their history,
3. offer to stop Claude Code deleting its own sessions (asks first),
4. connect the tools to the memory (asks first), repairing entries left by an older install,
5. show what is archived, how to add web chats, and what to do next.
"""

from __future__ import annotations

import argparse
import sys

from anamnesis.config import Settings

DOCS = "https://github.com/devHaitham481/anamnesis#readme"


def _ask(question: str, assume_yes: bool) -> bool | None:
    """Y/n prompt. None when nobody can answer (no terminal) and --yes wasn't given."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return None
    try:
        answer = input(f"{question} [Y/n] ").strip().lower()
    except EOFError:
        return None
    return answer in ("", "y", "yes")


def _heading(n: int, text: str) -> None:
    print(f"\n{n}. {text}")


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
    print("Anamnesis setup")
    print(f"Memory is stored in {settings.data_dir}")

    _heading(1, "AI tools on this computer")
    tools = targets()
    found = [t for t in tools.values() if t.installed()]
    for t in tools.values():
        mark = "✓" if t in found else "·"
        print(f"   {mark} {t.label:<16} {'found' if t in found else 'not installed'}")
    if not found:
        print("   None of the supported tools were found. You can still import ChatGPT,")
        print("   claude.ai or Gemini exports later with `anam import <file>`.")

    _heading(2, "Archiving your history")
    if args.skip_import:
        print("   skipped (--skip-import)")
    else:
        if not (settings.data_dir / "models").is_dir() and settings.semantic_search:
            print("   First run: downloading the search model (about 70 MB, once).")
        code = cmd_sync(settings, argparse.Namespace(watch=False, force=False, interval=30.0))
        if code != 0:
            print("   Import did not finish; run `anam sync` to try again.", file=sys.stderr)

    _heading(3, "Keeping your history")
    days = claude_code_cleanup_days()
    if deletes_history(days):
        print(f"   Claude Code deletes conversations after {days} days. Anamnesis keeps its own")
        print("   copy either way; turning the deletion off keeps them resumable in Claude Code.")
        answer = _ask("   Stop Claude Code deleting old conversations?", args.yes)
        if answer is None:
            print("   No terminal to ask in. Run `anam keep` to do it later.")
        elif not answer:
            print("   Left as is. Change your mind with `anam keep`.")
        else:
            try:
                print(f"   ✓ Claude Code      {keep_claude_code_history()}")
            except ValueError as e:
                print(f"   ✗ Claude Code      {e}", file=sys.stderr)
    else:
        print("   Nothing to change: Anamnesis keeps everything it archives, permanently.")

    _heading(4, "Connecting your AI tools")
    todo = [t for t in found if not t.current()]
    connected = [t for t in found if t not in todo]
    for t in connected:
        print(f"   ✓ {t.label:<16} already connected")
    failed = False
    if todo:
        names = ", ".join(t.label for t in todo)
        answer = _ask(f"   Give {names} access to your memory?", args.yes)
        if answer is None:
            print("   No terminal to ask in. Run `anam connect all` when ready.")
        elif not answer:
            print("   Skipped. Connect later with `anam connect all`.")
        else:
            for t in todo:
                try:
                    print(f"   ✓ {t.label:<16} {t.connect()}")
                    connected.append(t)
                except Exception as e:
                    print(f"   ✗ {t.label:<16} failed: {e}", file=sys.stderr)
                    failed = True
    elif not found:
        print("   Nothing to connect.")

    archive = archive_view.load(settings)
    print("\nDone. Your archive:\n")
    archive_view.print_archive(settings, archive, indent="   ", retention=False)
    missing = archive_view.missing_exports(archive)
    if missing:
        print("\nAdd your web chats (they live on the providers' servers, so export them once):")
        archive_view.print_exports(missing, indent="   ")

    print("\nNext:")
    if connected:
        names = ", ".join(t.label for t in connected)
        print(f"  • Restart {names} (or open a new session) so they load the memory.")
        print('  • Then ask about earlier work, e.g. "what did we decide about auth?"')
    print("  • Browse everything: anam        Search: anam search <words>")
    print("  • New history keeps syncing while a connected tool is running.")
    print("  • claude.ai and ChatGPT (web and phone): run your own node, see `anam node -h`.")
    print(f"  • Docs: {DOCS}")
    return 1 if failed else 0
