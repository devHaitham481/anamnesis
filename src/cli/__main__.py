"""`anam` command line.

anam                      open the terminal memory browser
anam sync [--watch]       import new history from Claude Code, Codex, Gemini CLI, Cursor
anam import FILE...       import a ChatGPT / claude.ai / Gemini (Takeout) data export
anam search QUERY         search all memory from the shell
anam connect [TOOL|all]   give AI tools access to the memory (MCP); no TOOL = show status
anam disconnect TOOL
anam status               what is stored, per source
anam mcp                  run the MCP server (started for you by connected tools)
anam node …               your self-hosted node: deploy, link, status, activity, revoke, wipe
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from src.config import Settings, load_settings


def _log_to_file(settings: Settings, name: str = "anamnesis.log") -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=settings.data_dir / name,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _with_service(settings: Settings, fn):  # type: ignore[no-untyped-def]
    from src.core.memory import MemoryService

    print("Opening memory…", file=sys.stderr, flush=True)
    service = await MemoryService.create(settings)
    try:
        return await fn(service)
    finally:
        await service.close()


def _print_report(report) -> None:  # type: ignore[no-untyped-def]
    from src.core.models import SOURCE_LABELS

    if report.messages_added:

        def label(k: str) -> str:
            if k.startswith("device:"):
                return f"from {k.removeprefix('device:')}"
            return SOURCE_LABELS.get(k, k)

        per = ", ".join(f"{label(k)}: {v}" for k, v in report.by_source.items())
        print(f"Imported {report.messages_added} new messages ({per}).")
    elif not report.shared:
        print("Memory is up to date.")
    if report.shared:
        print(f"Shared {report.shared} messages with your other computers.")
    for err in report.errors:
        print(f"  warning: {err}", file=sys.stderr)


def _progress(done: int, total: int) -> None:
    end = "\n" if done >= total else ""
    print(f"\rEmbedding for semantic search: {done}/{total}", end=end, flush=True)


def cmd_sync(settings: Settings, args: argparse.Namespace) -> int:
    from src.ingest.runner import exchange_with_devices, scan, sync_lock, watch

    async def run(service):  # type: ignore[no-untyped-def]
        with sync_lock(settings.data_dir) as acquired:
            if not acquired:
                print("Another Anamnesis process is syncing right now; try again shortly.")
                return 1
            report = await scan(service, force=args.force, index=False)
            await exchange_with_devices(service, report)
            _print_report(report)
            await service.backfill_index(progress=_progress)
        if args.watch:
            print(f"Watching for new conversations every {args.interval:.0f}s (Ctrl+C to stop)…")
            await watch(service, interval=args.interval)
        return 0

    try:
        return int(asyncio.run(_with_service(settings, run)))
    except KeyboardInterrupt:
        return 0


def cmd_import(settings: Settings, args: argparse.Namespace) -> int:
    from src.ingest.exports import ExportFormatError
    from src.ingest.runner import import_file

    paths = [Path(f).expanduser() for f in args.files]
    missing = [p for p in paths if not p.is_file()]
    for path in missing:
        print(f"{path}: file not found", file=sys.stderr)
    paths = [p for p in paths if p not in missing]

    async def run(service):  # type: ignore[no-untyped-def]
        code = 1 if missing else 0
        for path in paths:
            try:
                report = await import_file(service, path, index=False)
            except ExportFormatError as e:
                print(str(e), file=sys.stderr)
                code = 1
                continue
            print(
                f"{path.name}: {report.conversations} conversations, "
                f"{report.messages_added} new messages."
            )
        return code

    return int(asyncio.run(_with_service(settings, run)))


def cmd_search(settings: Settings, args: argparse.Namespace) -> int:
    from src.memory_mcp.server import format_hits

    async def run(service):  # type: ignore[no-untyped-def]
        hits = await service.search(" ".join(args.query), k=args.limit, source=args.source)
        print(format_hits(hits))

    asyncio.run(_with_service(settings, run))
    return 0


def cmd_status(settings: Settings, _: argparse.Namespace) -> int:
    from src.core.db import Database
    from src.core.models import SOURCE_LABELS

    async def run() -> None:
        async with Database(settings.db_path) as db:
            counts = await db.source_counts()
            total = await db.max_message_id()
        print(f"Memory: {settings.data_dir}")
        if not counts:
            print("  (empty) run `anam sync` to import your AI tools' history")
        for source, n in counts.items():
            print(f"  {SOURCE_LABELS.get(source, source):<22} {n:>5} conversations")
        print(f"  {'messages (total)':<22} {total:>5}")

    asyncio.run(run())
    print()
    cmd_devices(settings, argparse.Namespace())
    print()
    return cmd_connect(settings, argparse.Namespace(tool=None))


def cmd_connect(settings: Settings, args: argparse.Namespace) -> int:
    from src.cli.node_cmd import WEB_CONNECTORS, connect_web
    from src.connect import targets

    if args.tool in WEB_CONNECTORS:
        return connect_web(settings, args.tool)
    all_targets = targets()
    if not args.tool:
        print("AI tools (MCP access to memory):")
        for t in all_targets.values():
            state = (
                "connected"
                if t.connected()
                else "not connected"
                if t.installed()
                else "not installed"
            )
            print(f"  {t.name:<15} {t.label:<15} {state}")
        print("\nConnect with: anam connect <tool>   or   anam connect all")
        print("Web apps (through your node): anam connect claude-web | chatgpt-web")
        return 0
    if args.tool == "all":
        chosen = [t for t in all_targets.values() if t.installed()]
    elif args.tool in all_targets:
        chosen = [all_targets[args.tool]]
    else:
        print(
            f"Unknown tool {args.tool!r}. Choose from: {', '.join(all_targets)}, all, "
            + ", ".join(WEB_CONNECTORS),
            file=sys.stderr,
        )
        return 2
    code = 0
    for t in chosen:
        try:
            print(f"{t.label}: {t.connect()}")
        except Exception as e:
            print(f"{t.label}: failed: {e}", file=sys.stderr)
            code = 1
    return code


def cmd_disconnect(_: Settings, args: argparse.Namespace) -> int:
    from src.connect import targets

    t = targets().get(args.tool)
    if t is None:
        print(f"Unknown tool {args.tool!r}", file=sys.stderr)
        return 2
    print(f"{t.label}: {t.disconnect()}")
    return 0


def _folder(value: str | None) -> Path:
    from src.core.devices import default_folder

    return Path(value).expanduser().resolve() if value else default_folder()


def cmd_share(settings: Settings, args: argparse.Namespace) -> int:
    from src.core.devices import SyncSetupError, share

    folder = _folder(args.folder)
    try:
        code = share(settings.data_dir, folder)
    except SyncSetupError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"Memory sync is on. Folder: {folder}")
    print("\nPairing code (keep it private, it is the encryption key):\n")
    print(f"    {code}\n")
    print("On your other computer, once this folder has synced there:")
    print(f"    anam join {code}" + (f" {folder}" if args.folder else ""))
    print("\nRun `anam sync` (or just keep using your tools) to publish this computer's memory.")
    return 0


def cmd_join(settings: Settings, args: argparse.Namespace) -> int:
    from src.core.devices import SyncSetupError, join

    folder = _folder(args.folder)
    try:
        join(settings.data_dir, folder, args.code)
    except SyncSetupError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"Joined. Pulling memory from your other computers ({folder})…")
    code = cmd_sync(settings, argparse.Namespace(force=False, watch=False, interval=30.0))
    print("\nNext: `anam connect all` so this computer's AI tools can use the memory.")
    return code


def cmd_devices(settings: Settings, _: argparse.Namespace) -> int:
    import datetime as dt

    from src.core.devices import list_devices, load_config, this_device

    cfg = load_config(settings.data_dir)
    if cfg is None:
        print(
            "Memory sync is off. Link a node with `anam node link <url>`, or sync through a "
            "folder with `anam share`."
        )
        return 0
    me = this_device(settings.data_dir)
    print(f"Synced via {'your node ' if cfg.node else ''}{cfg.where}")
    try:
        found = list_devices(cfg)
    except OSError as e:
        print(f"  (can't reach it right now: {e})")
        return 0
    for d in found:
        when = (
            dt.datetime.fromtimestamp(d["last_write"]).strftime("%Y-%m-%d %H:%M")
            if d["last_write"]
            else "never"
        )
        mark = "  (this computer)" if d["id"] == me.id else ""
        print(f"  {d['name']:<24} last update {when}{mark}")
    return 0


def cmd_unshare(settings: Settings, _: argparse.Namespace) -> int:
    from src.core.devices import disable

    print(
        "Memory sync turned off for this computer."
        if disable(settings.data_dir)
        else "Memory sync was not on."
    )
    return 0


def cmd_mcp(_: Settings, __: argparse.Namespace) -> int:
    from src.memory_mcp.server import main as mcp_main

    mcp_main()
    return 0


def cmd_tui(settings: Settings, _: argparse.Namespace) -> int:
    _log_to_file(settings)  # never log to the terminal while the TUI owns it
    from src.cli.app import AnamnesisApp

    AnamnesisApp(settings).run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anam", description="Shared local memory for all your AI tools."
    )
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("sync", help="import new history from local AI tools")
    p.add_argument("--watch", action="store_true", help="keep running and import continuously")
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--force", action="store_true", help="re-read every file")
    p = sub.add_parser("import", help="import ChatGPT / claude.ai / Gemini Takeout exports")
    p.add_argument("files", nargs="+")
    p = sub.add_parser("search", help="search memory")
    p.add_argument("query", nargs="+")
    p.add_argument("--source")
    p.add_argument("--limit", type=int, default=8)
    p = sub.add_parser("connect", help="give AI tools access to memory via MCP")
    p.add_argument("tool", nargs="?")
    p = sub.add_parser("disconnect", help="remove Anamnesis from an AI tool")
    p.add_argument("tool")
    p = sub.add_parser("share", help="sync memory across computers through a synced folder")
    p.add_argument("folder", nargs="?", help="default: iCloud Drive/Anamnesis")
    p = sub.add_parser("join", help="add this computer to memory shared from another one")
    p.add_argument("code")
    p.add_argument("folder", nargs="?", help="default: iCloud Drive/Anamnesis")
    sub.add_parser("devices", help="computers sharing this memory")
    sub.add_parser("unshare", help="stop syncing memory on this computer")
    sub.add_parser("status", help="what is stored, and which tools are connected")
    sub.add_parser("mcp", help="run the MCP server on stdio")
    sub.add_parser("tui", help="open the terminal UI (default)")
    from src.cli import node_cmd

    node_cmd.add_parser(sub)
    return parser


COMMANDS = {
    "sync": cmd_sync,
    "import": cmd_import,
    "search": cmd_search,
    "connect": cmd_connect,
    "disconnect": cmd_disconnect,
    "status": cmd_status,
    "share": cmd_share,
    "join": cmd_join,
    "devices": cmd_devices,
    "unshare": cmd_unshare,
    "mcp": cmd_mcp,
    "tui": cmd_tui,
    None: cmd_tui,
}


def cmd_node(settings: Settings, args: argparse.Namespace) -> int:
    from src.cli import node_cmd

    return node_cmd.run(settings, args)


COMMANDS["node"] = cmd_node


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    serving = args.command == "node" and args.node_command == "serve"
    if args.command not in ("tui", None, "mcp") and not serving:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return COMMANDS[args.command](settings, args)


if __name__ == "__main__":
    sys.exit(main())
