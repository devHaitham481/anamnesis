"""Register NexusAI's MCP memory server with the AI tools installed on this machine.

Every edit is a merge (other servers/settings are kept) and a `.bak` copy is written first.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.ingest.codex import codex_home
from src.ingest.cursor import cursor_user_dir

SERVER_NAME = "nexus"


def server_command() -> tuple[str, list[str]]:
    """Absolute interpreter path: GUI apps (Claude Desktop, Cursor) don't inherit your PATH."""
    return sys.executable, ["-m", "src.memory_mcp"]


@dataclass
class Target:
    name: str
    label: str
    installed: Callable[[], bool]
    connected: Callable[[], bool]
    connect: Callable[[], str]
    disconnect: Callable[[], str]


# ---------- JSON "mcpServers" files (Claude Desktop, Cursor, Gemini CLI) ----------


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or not path.read_text(encoding="utf-8").strip():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object; not touching it")
    return data


def _backup(path: Path) -> None:
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak"))


def _json_connected(path: Path) -> bool:
    try:
        return SERVER_NAME in (_read_json(path).get("mcpServers") or {})
    except (ValueError, OSError):
        return False


def _json_connect(path: Path, extra: dict[str, Any] | None = None) -> str:
    data = _read_json(path)
    command, args = server_command()
    servers = data.setdefault("mcpServers", {})
    servers[SERVER_NAME] = {"command": command, "args": args, **(extra or {})}
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"added '{SERVER_NAME}' to {path}"


def _json_disconnect(path: Path) -> str:
    data = _read_json(path)
    if SERVER_NAME not in (data.get("mcpServers") or {}):
        return "not connected"
    _backup(path)
    del data["mcpServers"][SERVER_NAME]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"removed '{SERVER_NAME}' from {path}"


# ---------- Codex (TOML) ----------


def _codex_config() -> Path:
    return codex_home() / "config.toml"


def _codex_connected() -> bool:
    path = _codex_config()
    if not path.is_file():
        return False
    try:
        return SERVER_NAME in tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers", {})
    except tomllib.TOMLDecodeError:
        return False


def _codex_connect() -> str:
    if _codex_connected():
        return "already connected"
    path = _codex_config()
    command, args = server_command()
    block = (
        f"\n[mcp_servers.{SERVER_NAME}]\n"
        f"command = {json.dumps(command)}\n"
        f"args = {json.dumps(args)}\n"
    )
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return f"added [mcp_servers.{SERVER_NAME}] to {path}"


def _codex_disconnect() -> str:
    path = _codex_config()
    if not _codex_connected():
        return "not connected"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out, skipping = [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = stripped in (
                f"[mcp_servers.{SERVER_NAME}]",
                f"[mcp_servers.{SERVER_NAME}.env]",
            )
        if not skipping:
            out.append(line)
    _backup(path)
    path.write_text("".join(out), encoding="utf-8")
    return f"removed [mcp_servers.{SERVER_NAME}] from {path}"


# ---------- Claude Code (via its own CLI) ----------


def _claude_bin() -> str | None:
    return shutil.which("claude") or (
        str(Path.home() / ".local/bin/claude")
        if (Path.home() / ".local/bin/claude").exists()
        else None
    )


def _claude_run(*args: str) -> subprocess.CompletedProcess[str]:
    exe = _claude_bin()
    if exe is None:
        raise FileNotFoundError("the `claude` CLI is not on PATH")
    return subprocess.run([exe, "mcp", *args], capture_output=True, text=True, timeout=60)


def _claude_code_connected() -> bool:
    try:
        return _claude_run("get", SERVER_NAME).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _claude_code_connect() -> str:
    if _claude_code_connected():
        return "already connected"
    command, args = server_command()
    res = _claude_run("add", "--scope", "user", SERVER_NAME, "--", command, *args)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip())
    return "registered with `claude mcp add --scope user` (all projects)"


def _claude_code_disconnect() -> str:
    res = _claude_run("remove", "--scope", "user", SERVER_NAME)
    return "removed" if res.returncode == 0 else (res.stderr or res.stdout).strip()


# ---------- registry ----------


def _claude_desktop_config() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform.startswith("win"):
        return Path.home() / "AppData/Roaming/Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def targets() -> dict[str, Target]:
    desktop = _claude_desktop_config()
    cursor = Path.home() / ".cursor" / "mcp.json"
    gemini = Path.home() / ".gemini" / "settings.json"
    return {
        "claude-code": Target(
            "claude-code",
            "Claude Code",
            lambda: _claude_bin() is not None,
            _claude_code_connected,
            _claude_code_connect,
            _claude_code_disconnect,
        ),
        "claude-desktop": Target(
            "claude-desktop",
            "Claude Desktop",
            lambda: desktop.parent.is_dir(),
            lambda: _json_connected(desktop),
            lambda: _json_connect(desktop) + " (restart Claude Desktop)",
            lambda: _json_disconnect(desktop),
        ),
        "cursor": Target(
            "cursor",
            "Cursor",
            lambda: cursor.parent.is_dir() or cursor_user_dir().is_dir(),
            lambda: _json_connected(cursor),
            lambda: _json_connect(cursor),
            lambda: _json_disconnect(cursor),
        ),
        "gemini-cli": Target(
            "gemini-cli",
            "Gemini CLI",
            lambda: gemini.parent.is_dir() or shutil.which("gemini") is not None,
            lambda: _json_connected(gemini),
            lambda: _json_connect(gemini, {"timeout": 30000}),
            lambda: _json_disconnect(gemini),
        ),
        "codex": Target(
            "codex",
            "Codex CLI",
            lambda: codex_home().is_dir() or shutil.which("codex") is not None,
            _codex_connected,
            _codex_connect,
            _codex_disconnect,
        ),
    }
