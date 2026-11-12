"""`anam node …`: run, deploy, link and manage your self-hosted Anamnesis node.

Also `anam connect claude-web` / `chatgpt-web`, which explain how to add the node as a connector.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from anamnesis.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # the repo root, when run from a checkout
# Published by .github/workflows/docker.yml on every change to main.
DEFAULT_IMAGE = "ghcr.io/devhaitham481/anamnesis:latest"
FLY_TOML = """\
app = "{app}"
primary_region = "{region}"

[build]
{build}

[env]
  ANAMNESIS_DATA_DIR = "/data"
  PORT = "8080"

[mounts]
  source = "anamnesis_data"
  destination = "/data"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 0

  [[http_service.checks]]
    path = "/healthz"
    interval = "30s"
    timeout = "5s"
    grace_period = "90s"

[[vm]]
  memory = "1gb"
  cpu_kind = "shared"
  cpus = 1
"""

WEB_CONNECTORS = {
    "claude-web": (
        "claude.ai (also Claude Desktop and the mobile apps)",
        [
            "Open claude.ai → Settings → Connectors → Add custom connector.",
            "Name: Anamnesis   URL: {url}/mcp",
            "Click Connect. Your node's sign-in page opens: enter the node password.",
            "Tick “Also allow it to save notes” if Claude should be able to save to memory.",
        ],
    ),
    "chatgpt-web": (
        "ChatGPT",
        [
            "Open ChatGPT → Settings → Apps / Connectors. Custom connectors may need "
            "Developer mode (under Advanced) and a paid plan.",
            "Create a connector. Name: Anamnesis   URL: {url}/mcp   Authentication: OAuth",
            "Connect, then sign in on your node's page with the node password.",
            "Tick “Also allow it to save notes” if ChatGPT should be able to save to memory.",
        ],
    ),
}


def _err(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 1


# ---------- node config on this computer ----------


def _node(settings: Settings) -> Any:
    """(config, NodeStore) for the node this computer is linked to, or None."""
    from anamnesis.core.devices import NodeStore, load_config

    cfg = load_config(settings.data_dir)
    if cfg is None or cfg.node is None:
        return None
    return cfg, NodeStore(cfg.node, cfg.key)


def _admin(settings: Settings, method: str, path: str, data: Any = None) -> Any:
    linked = _node(settings)
    if linked is None:
        raise SystemExit("This computer is not linked to a node. Run `anam node link <url>` first.")
    _, store = linked
    try:
        return store.api(method, path, data)
    except OSError as e:
        if "401" in str(e):
            raise SystemExit(
                "The node no longer accepts this computer (was it wiped?). "
                "Run `anam node link <url>` again."
            ) from None
        raise SystemExit(f"Could not reach your node: {e}") from None


def _when(ts: float | None) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"


# ---------- scope ----------


def _ask_scope(args: argparse.Namespace) -> Any:
    from anamnesis.core.devices import Scope

    excludes = tuple(str(Path(p).expanduser().resolve()) for p in (args.exclude or []))
    if args.notes_only:
        return Scope("notes", excludes)
    if excludes or args.everything or not sys.stdin.isatty():
        return Scope("all", excludes)
    print("What should this computer upload to your node?")
    print("  1) Everything (recommended): web chats can recall all your coding sessions")
    print("  2) Everything except some projects")
    print("  3) Only notes saved with `remember`")
    choice = input("Choose 1, 2 or 3 [1]: ").strip() or "1"
    if choice == "3":
        return Scope("notes")
    if choice == "2":
        raw = input("Project folders to leave out (comma-separated): ")
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        paths = tuple(str(Path(p).expanduser().resolve()) for p in parts)
        return Scope("all", paths)
    return Scope("all")


def _password(prompt: str = "Node password: ") -> str:
    env = os.environ.get("ANAMNESIS_NODE_PASSWORD")
    if env:
        return env
    return getpass.getpass(prompt)


# ---------- commands ----------


def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    from anamnesis.node.app import main

    main(host=args.host, port=args.port)
    return 0


def cmd_link(settings: Settings, args: argparse.Namespace) -> int:
    from anamnesis.cli.__main__ import cmd_sync
    from anamnesis.core.devices import SyncSetupError, link_node, load_config

    previous = load_config(settings.data_dir)
    try:
        password = _password()
        scope = _ask_scope(args)
        cfg = link_node(settings.data_dir, args.url, password, scope)
    except SyncSetupError as e:
        return _err(str(e))
    except (KeyboardInterrupt, EOFError):
        return 1
    print(f"\nLinked to {cfg.node}. Uploading: {scope.describe()}.")
    if previous is not None and previous.node is None:
        print(
            f"(This replaces folder sync via {previous.folder}. Link your other computers to the "
            "node too.)"
        )
    print("Syncing now…")
    code = cmd_sync(settings, argparse.Namespace(force=False, watch=False, interval=30.0))
    print(
        "\nNext:\n"
        "  anam node link " + str(cfg.node) + "    on your other computers\n"
        "  anam connect claude-web            to use your memory in claude.ai\n"
        "  anam connect chatgpt-web           to use it in ChatGPT"
    )
    return code


def cmd_scope(settings: Settings, args: argparse.Namespace) -> int:
    from anamnesis.core.devices import SyncSetupError, load_config, set_scope

    cfg = load_config(settings.data_dir)
    if cfg is None:
        return _err("Memory sync is off.")
    if not (args.notes_only or args.everything or args.exclude):
        print(f"This computer uploads: {cfg.scope.describe()}.")
        print("Change it with --everything, --notes-only or --exclude PATH.")
        return 0
    try:
        cfg = set_scope(settings.data_dir, _ask_scope(args))
    except SyncSetupError as e:
        return _err(str(e))
    print(f"This computer now uploads: {cfg.scope.describe()}. Applies from the next sync.")
    if cfg.scope.mode == "notes" or cfg.scope.exclude_projects:
        print("Anything already uploaded stays on the node; use `anam node wipe` to start over.")
    return 0


def cmd_status(settings: Settings, _: argparse.Namespace) -> int:
    from anamnesis.core.models import SOURCE_LABELS

    linked = _node(settings)
    if linked is None:
        print("Not linked to a node. Deploy one (`anam node deploy fly`, or Docker), then")
        print("run `anam node link <url>`.")
        return 0
    cfg, _ = linked
    info = _admin(settings, "GET", "/admin/status")
    print(f"Node: {info['url']}")
    print(f"This computer uploads: {cfg.scope.describe()}")
    print(f"Search: {'semantic + keyword' if info['semantic'] else 'keyword only'}")
    print("\nMemory on the node:")
    for source, n in info["sources"].items():
        print(f"  {SOURCE_LABELS.get(source, source):<26} {n:>5} conversations")
    if not info["sources"]:
        print("  (empty)")
    print("\nComputers:")
    for d in info["devices"]:
        if d["name"] == "node":
            continue  # the node's own stream: notes saved by web apps
        print(f"  {d['name']:<26} last upload {_when(d['last_write'])}")
    print("\nWeb apps with access:")
    for c in info["connections"]:
        mode = "search + save" if c["can_save"] else "search only"
        print(f"  {c['name']:<26} {mode:<14} last used {_when(c['last_used'])}")
    if not info["connections"]:
        print("  none yet: `anam connect claude-web` or `anam connect chatgpt-web`")
    return 0


def cmd_activity(settings: Settings, args: argparse.Namespace) -> int:
    rows = _admin(settings, "GET", f"/admin/activity?limit={args.limit}")
    if not rows:
        print("No activity yet.")
        return 0
    verbs = {
        "search_memory": "searched",
        "recent_conversations": "listed recent",
        "get_conversation": "read",
        "remember": "saved",
    }
    for r in reversed(rows):
        verb = verbs.get(r["tool"], r["tool"])
        print(f"{_when(r['ts'])}  {r['client']:<16} {verb}: {r['detail']}")
    return 0


def cmd_revoke(settings: Settings, args: argparse.Namespace) -> int:
    if not args.client and not args.all:
        info = _admin(settings, "GET", "/admin/status")
        names = ", ".join(c["name"] for c in info["connections"]) or "none"
        return _err(f"Name an app to revoke (connected: {names}), or use --all.")
    n = _admin(settings, "POST", "/admin/revoke", {"client": None if args.all else args.client})
    if not n["revoked"]:
        return _err(f"No connected app called {args.client!r}.")
    print(f"Revoked {n['revoked']} app(s). They must sign in again to use your memory.")
    return 0


def cmd_wipe(settings: Settings, args: argparse.Namespace) -> int:
    from anamnesis.core.devices import disable

    linked = _node(settings)
    if linked is None:
        return _err("This computer is not linked to a node.")
    cfg, _ = linked
    if not args.yes:
        print(f"This erases ALL memory on {cfg.node}, disconnects every web app, and makes every")
        print("computer link again. Your computers keep their own copies.")
        if input("Type 'wipe' to continue: ").strip() != "wipe":
            print("Cancelled.")
            return 1
    _admin(settings, "POST", "/admin/wipe", {"confirm": "wipe"})
    disable(settings.data_dir)
    print(f"Node wiped. Link again with: anam node link {cfg.node}")
    return 0


# ---------- deploy ----------


def _run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    print("  $ " + " ".join(c if "PASSWORD" not in c else c.split("=")[0] + "=…" for c in cmd))
    return subprocess.run(cmd, text=True, **kw)


def cmd_deploy(settings: Settings, args: argparse.Namespace) -> int:
    if args.target != "fly":
        return _err("Only `fly` is automated. For any other server, see the Docker guide.")
    fly = shutil.which("fly") or shutil.which("flyctl")
    if fly is None:
        return _err(
            "Install the Fly.io CLI first: https://fly.io/docs/flyctl/install/ "
            "(macOS: `brew install flyctl`), then `fly auth login`."
        )
    if subprocess.run([fly, "auth", "whoami"], capture_output=True).returncode != 0:
        return _err("Sign in to Fly.io first: `fly auth login`.")
    image = args.image
    if image is None and not (args.build and (PROJECT_ROOT / "Dockerfile").is_file()):
        if args.build:
            return _err("--build needs a checkout of the repository (no Dockerfile found).")
        image = DEFAULT_IMAGE

    app = args.app or f"anamnesis-{secrets.token_hex(3)}"
    url = f"https://{app}.fly.dev"
    print(f"Deploying your node as {app} ({url}) in region {args.region}.\n")
    password = os.environ.get("ANAMNESIS_NODE_PASSWORD") or getpass.getpass(
        "Choose a node password (12+ characters; leave empty to generate one): "
    )
    generated = not password
    if generated:
        password = secrets.token_urlsafe(18)
    elif len(password) < 12:
        return _err("The password must be at least 12 characters.")

    steps = [
        [fly, "apps", "create", app],
        [
            fly,
            "volumes",
            "create",
            "anamnesis_data",
            "--app",
            app,
            "--region",
            args.region,
            "--size",
            "1",
            "--yes",
        ],
        [fly, "secrets", "set", f"ANAMNESIS_NODE_PASSWORD={password}", "--app", app, "--stage"],
    ]
    for step in steps:
        if _run(step).returncode != 0:
            return _err("Fly.io step failed (see above). Nothing else was changed.")
    build = f'  image = "{image}"' if image else '  dockerfile = "Dockerfile"'
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "fly.toml"
        config.write_text(FLY_TOML.format(app=app, region=args.region, build=build))
        deploy = [fly, "deploy", "--app", app, "--config", str(config), "--ha=false"]
        if image is None:
            deploy.append(str(PROJECT_ROOT))
        if _run(deploy).returncode != 0:
            return _err(f"Deploy failed. Retry with: anam node deploy fly --app {app}")

    print(f"\nYour node is live at {url}")
    if generated:
        print(f"Node password (save it in your password manager now):\n\n    {password}\n")
    if args.no_link:
        print(f"Link this computer with: anam node link {url}")
        return 0
    os.environ["ANAMNESIS_NODE_PASSWORD"] = password  # reuse for the link step only
    try:
        link_args = argparse.Namespace(
            url=url, notes_only=args.notes_only, everything=False, exclude=args.exclude
        )
        return cmd_link(settings, link_args)
    finally:
        os.environ.pop("ANAMNESIS_NODE_PASSWORD", None)


# ---------- web connectors ----------


def connect_web(settings: Settings, tool: str) -> int:
    label, steps = WEB_CONNECTORS[tool]
    linked = _node(settings)
    if linked is None:
        print(f"{label} reaches your memory through your Anamnesis node, which stays online")
        print("while your computers sleep. Set one up first:\n")
        print("  anam node deploy fly         (or Docker on any server: see the README)")
        print("  anam node link <node-url>")
        return 1
    cfg, _ = linked
    print(f"Connect {label} to your memory:\n")
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step.format(url=cfg.node)}")
    print("\nCheck it worked: `anam node status`. See what it does: `anam node activity`.")
    print(f"Disconnect any time: `anam node revoke {tool.split('-')[0]}`.")
    return 0


# ---------- parser ----------


def add_parser(sub: Any) -> None:
    p = sub.add_parser("node", help="your self-hosted, always-on node (web chats, sync hub)")
    nsub = p.add_subparsers(dest="node_command", required=True)

    s = nsub.add_parser("serve", help="run the node (what the Docker image runs)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int)

    def scope_flags(q: Any) -> None:
        g = q.add_mutually_exclusive_group()
        g.add_argument("--everything", action="store_true", help="upload all memory")
        g.add_argument("--notes-only", action="store_true", help="upload only saved notes")
        q.add_argument(
            "--exclude", action="append", metavar="PATH", help="leave out a project folder"
        )

    s = nsub.add_parser("link", help="sync this computer through a node")
    s.add_argument("url")
    scope_flags(s)
    s = nsub.add_parser("scope", help="show or change what this computer uploads")
    scope_flags(s)
    nsub.add_parser("status", help="memory, computers and web apps on your node")
    s = nsub.add_parser("activity", help="what web apps searched or saved")
    s.add_argument("--limit", type=int, default=30)
    s = nsub.add_parser("revoke", help="cut off a web app (e.g. claude, chatgpt)")
    s.add_argument("client", nargs="?")
    s.add_argument("--all", action="store_true")
    s = nsub.add_parser("wipe", help="erase everything on the node and start over")
    s.add_argument("--yes", action="store_true")
    s = nsub.add_parser("deploy", help="create your node on Fly.io")
    s.add_argument("target", choices=["fly"])
    s.add_argument("--app", help="Fly app name (default: anamnesis-<random>)")
    s.add_argument("--region", default="fra", help="Fly region code (default: fra)")
    s.add_argument("--image", help=f"image to deploy (default: {DEFAULT_IMAGE})")
    s.add_argument("--build", action="store_true", help="build this checkout instead")
    s.add_argument("--no-link", action="store_true", help="don't link this computer afterwards")
    s.add_argument("--notes-only", action="store_true")
    s.add_argument("--exclude", action="append", metavar="PATH")


NODE_COMMANDS = {
    "serve": cmd_serve,
    "link": cmd_link,
    "scope": cmd_scope,
    "status": cmd_status,
    "activity": cmd_activity,
    "revoke": cmd_revoke,
    "wipe": cmd_wipe,
    "deploy": cmd_deploy,
}


def run(settings: Settings, args: argparse.Namespace) -> int:
    return NODE_COMMANDS[args.node_command](settings, args)
