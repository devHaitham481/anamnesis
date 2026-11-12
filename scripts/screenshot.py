"""Render docs/screenshot.svg: the terminal UI over a small *sample* memory.

The conversations below are made up, so no real history ends up in the repo. Re-run after
UI changes:  python scripts/screenshot.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.cli.app import AnamnesisApp  # noqa: E402
from src.config import Settings  # noqa: E402
from src.core.db import Database  # noqa: E402
from src.core.memory import MemoryService  # noqa: E402
from src.core.models import ImportedConversation, ImportedMessage  # noqa: E402
from src.core.vector import HashEmbedder, VectorIndex  # noqa: E402

DAY = 86_400.0
NOW = 1_794_000_000.0

SAMPLE: list[tuple[str, str, str | None, float, list[str]]] = [
    # (source, title, project, days ago, messages alternating user/assistant)
    ("chatgpt", "Auth for the API: sessions or JWT?", None, 62, [
        "For the API, should we use JWTs or server-side session cookies?",
        "Given you need instant revocation when a user is removed, use session cookies. "
        "With JWTs you'd end up keeping a denylist anyway, which is a session store.",
        "Ok, session cookies it is. Redis or Postgres for the session store?",
        "Postgres: you already run it, and the load is a few hundred lookups a second.",
    ]),
    ("claude-code", "Add session middleware", "~/code/api-server", 0.2, [
        "Add the session middleware we discussed. Check memory for what we decided.",
        "From memory (ChatGPT, Sep 5): session cookies, not JWT, stored in Postgres, "
        "because removed users must lose access immediately. Adding `SessionMiddleware` "
        "with a `sessions` table and a 30-day sliding expiry.",
        "Also make the cookie SameSite=Lax and Secure.",
        "Done: cookie is HttpOnly, Secure, SameSite=Lax. Tests in tests/test_sessions.py pass.",
    ]),
    ("cursor", "Rate limiting on /login", "~/code/api-server", 9, [
        "Rate limit /login per IP and per account.",
        "Added a token bucket: 5 attempts per minute per IP, 20 per hour per account, "
        "stored next to the sessions table.",
    ]),
    ("codex", "Migrate CI to GitHub Actions", "~/code/api-server", 14, [
        "Port the CI pipeline from CircleCI to GitHub Actions.",
        "Created .github/workflows/ci.yml with lint, type-check and tests on 3.11-3.13.",
    ]),
    ("gemini-cli", "Postgres connection pool sizing", "~/code/api-server", 21, [
        "What pool size for 4 web workers on a 2 vCPU Postgres?",
        "Start with 5 connections per worker (20 total) and watch for waits.",
    ]),
    ("claude-web", "Pricing page copy", None, 30, [
        "Help me write the pricing page for the hosted plan.",
        "Here is a draft with three tiers…",
    ]),
    ("note", "Notes: api-server", None, 40, [
        "Decision: sessions are revoked on password change and on role removal.",
    ]),
]


def conversation(i: int, source: str, title: str, project: str | None, ago: float,
                 texts: list[str]) -> ImportedConversation:
    start = NOW - ago * DAY
    return ImportedConversation(
        source=source, external_id=f"sample-{i}", title=title, project=project,
        messages=[
            ImportedMessage(external_id=f"sample-{i}-{j}",
                            role="user" if j % 2 == 0 else "assistant",
                            content=t, created_at=start + j * 60)
            for j, t in enumerate(texts)
        ],
    )


async def main(out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(data_dir=Path(tmp), memory_sync=False, sync_interval=3600)
        db = await Database(settings.db_path).open()
        service = MemoryService(db, VectorIndex(settings.vector_path, HashEmbedder()), settings)
        for i, (source, title, project, ago, texts) in enumerate(SAMPLE):
            await service.import_conversation(conversation(i, source, title, project, ago, texts))

        app = AnamnesisApp(settings, service)
        async with app.run_test(size=(118, 34)) as pilot:
            for _ in range(100):
                if app.thread_id is not None and not app.query_one("#prompt").disabled:
                    break
                await pilot.pause(0.05)
            await pilot.pause(0.3)
            app.save_screenshot(filename=out.name, path=str(out.parent))
        await service.close()
    print(f"wrote {out}")


if __name__ == "__main__":
    target = ROOT / "docs" / "screenshot.svg"
    target.parent.mkdir(exist_ok=True)
    asyncio.run(main(target))
