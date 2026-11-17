"""Build a *sample* memory for the README demo GIF (docs/demo.tape): no real history.

    python scripts/demo_data.py /tmp/anam-demo

Creates <dir>/data (an Anamnesis data dir with made-up conversations, embedded with the real
model when it is already downloaded) and <dir>/claude (a stand-in Claude Code config dir).
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from screenshot import SAMPLE, conversation  # noqa: E402

from anamnesis.config import Settings  # noqa: E402
from anamnesis.core.memory import MemoryService  # noqa: E402

NOW = time.time()
DAY = 86_400.0

MORE: list[tuple[str, str, str | None, float, list[str]]] = [
    (
        "claude-code",
        "Upgrade to Postgres 17",
        "~/code/api-server",
        6.5,
        [
            "Plan the Postgres 16 to 17 upgrade.",
            "pg_upgrade with --link on a snapshot first; the only breaking change for us is the "
            "removed old_snapshot_threshold setting.",
        ],
    ),
    (
        "gemini-web",
        "Explain OAuth PKCE",
        None,
        23,
        [
            "Explain PKCE like I'm a backend developer.",
            "The client sends a hash of a random secret first, and the secret itself when it "
            "redeems the code, so a stolen code alone is useless.",
        ],
    ),
    (
        "claude-code",
        "Session cleanup job",
        "~/code/api-server",
        8.2,
        [
            "Expired sessions pile up in Postgres. Clean them.",
            "Added a nightly job that deletes sessions idle for 30 days, in batches of 5,000.",
        ],
    ),
    (
        "codex",
        "Flaky Redis test",
        "~/code/web-app",
        11,
        [
            "test_cache_eviction fails randomly.",
            "It depended on wall-clock TTLs; it now uses a fake clock.",
        ],
    ),
    (
        "chatgpt",
        "Name for the CLI",
        None,
        40,
        [
            "Short names for a memory CLI?",
            "anam, recall, mnem, trove…",
        ],
    ),
    (
        "claude-code",
        "Feature flags for checkout",
        "~/code/web-app",
        14,
        [
            "Put the new checkout behind a flag.",
            "Added a `checkout_v2` flag, on for staff only; the old flow stays the default.",
        ],
    ),
    (
        "cursor",
        "Accessible color tokens",
        "~/code/web-app",
        19,
        [
            "Check the dashboard colors for contrast.",
            "Two tokens failed AA on dark backgrounds; adjusted them to 4.6:1 and 5.1:1.",
        ],
    ),
    (
        "claude-web",
        "Incident write-up: login outage",
        None,
        27,
        [
            "Help me write the postmortem for Tuesday's login outage.",
            "Root cause: the session table hit its connection limit during a deploy…",
        ],
    ),
]


def main(out: Path) -> None:
    data, claude = out / "data", out / "claude"
    data.mkdir(parents=True, exist_ok=True)
    (claude / "projects").mkdir(parents=True, exist_ok=True)
    real_models = Path.home() / ".anamnesis" / "models"
    if real_models.is_dir() and not (data / "models").exists():
        os.symlink(real_models, data / "models")  # reuse the downloaded model

    async def build() -> None:
        settings = Settings(data_dir=data, memory_sync=False, sync_interval=3600)
        svc = await MemoryService.create(settings)
        for i, (source, title, project, ago, texts) in enumerate(SAMPLE + MORE):
            conv = conversation(i, source, title, project, ago, texts)
            await svc.import_conversation(conv)
        await svc.backfill_index()
        for i in range(27):  # agent lookups this week, for `anam status`
            await svc.record_lookup("search_memory", f"demo {i}")
        await svc.close()

    asyncio.run(build())
    # Sessions Claude Code has since deleted, but the archive kept.
    db = sqlite3.connect(data / "anamnesis.db")
    db.executemany(
        "INSERT OR REPLACE INTO ingest_state (path, signature, updated_at) VALUES (?, ?, ?)",
        [(f"claude-code:/gone/session-{i}.jsonl", "x", NOW) for i in range(14)],
    )
    db.commit()
    db.close()
    # The demo's Claude Code is connected to this install.
    entry = {
        "type": "stdio",
        "command": sys.argv[2] if len(sys.argv) > 2 else sys.executable,
        "args": ["-m", "anamnesis.memory_mcp"],
        "env": {},
    }
    (claude / ".claude.json").write_text(json.dumps({"mcpServers": {"anamnesis": entry}}))
    print(f"sample memory in {data}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
