"""`anam setup`: the guided first run (and safe to re-run after every upgrade).

1. find the AI tools on this computer,
2. import their history,
3. connect them to the memory (asks first), repairing entries left by an older install,
4. say what to do next.
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
    from anamnesis.cli.__main__ import cmd_sync
    from anamnesis.connect import targets

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

    _heading(2, "Importing your history")
    if args.skip_import:
        print("   skipped (--skip-import)")
    else:
        if not (settings.data_dir / "models").is_dir() and settings.semantic_search:
            print("   First run: downloading the search model (about 70 MB, once).")
        code = cmd_sync(settings, argparse.Namespace(watch=False, force=False, interval=30.0))
        if code != 0:
            print("   Import did not finish; run `anam sync` to try again.", file=sys.stderr)

    _heading(3, "Connecting your AI tools")
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

    print("\nDone.\n")
    print("Next:")
    if connected:
        names = ", ".join(t.label for t in connected)
        print(f"  • Restart {names} (or open a new session) so they load the memory.")
        print('  • Then ask about earlier work, e.g. "what did we decide about auth?"')
    print("  • Browse everything: anam        Search: anam search <words>")
    print("  • New history keeps syncing while a connected tool is running.")
    print("  • claude.ai and ChatGPT (web and phone): run your own node, see `anam node -h`.")
    print(f"  • Docs: {DOCS}")
    return 1 if failed else 0
