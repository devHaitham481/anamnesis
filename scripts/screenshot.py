"""Render docs/screenshot.svg: the terminal UI over a small *sample* memory.

The conversations below are made up, so no real history ends up in the repo. Re-run after
UI changes:  python scripts/screenshot.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from textual.widgets import Input  # noqa: E402

from anamnesis.cli.app import AnamnesisApp  # noqa: E402
from anamnesis.config import Settings  # noqa: E402
from anamnesis.core.db import Database  # noqa: E402
from anamnesis.core.memory import MemoryService  # noqa: E402
from anamnesis.core.models import ImportedConversation, ImportedMessage  # noqa: E402
from anamnesis.core.vector import HashEmbedder, VectorIndex  # noqa: E402

DAY = 86_400.0
NOW = time.time()  # sample dates are relative to today, so "2h ago" reads naturally

SAMPLE: list[tuple[str, str, str | None, float, list[str]]] = [
    # (source, title, project, days ago, messages alternating user/assistant)
    ("chatgpt", "Auth for the API: sessions or JWT?", None, 9, [
        "For the API, should we use JWTs or server-side session cookies?",
        "Given you need instant revocation when a user is removed, use session cookies. "
        "With JWTs you'd end up keeping a denylist anyway, which is a session store.",
        "Ok, session cookies it is. Redis or Postgres for the session store?",
        "Postgres: you already run it, and the load is a few hundred lookups a second.",
    ]),
    ("claude-code", "Add session middleware", "~/code/api-server", 0.09, [
        "Add the session middleware we discussed. Check memory for what we decided.",
        "Found it in memory (**ChatGPT**, 9 days ago): session cookies rather than JWT, "
        "stored in Postgres, because removed users must lose access immediately.\n\n"
        "Adding `SessionMiddleware` with a `sessions` table and a 30-day sliding expiry.",
        "Also make the cookie SameSite=Lax and Secure.",
        "Done. The cookie is `HttpOnly`, `Secure` and `SameSite=Lax`; "
        "`tests/test_sessions.py` passes (12 tests).",
    ]),
    ("cursor", "Rate limiting on /login", "~/code/api-server", 0.9, [
        "Rate limit /login per IP and per account.",
        "Added a token bucket: 5 attempts per minute per IP, 20 per hour per account.",
    ]),
    ("note", "Notes: api-server", None, 1.4, [
        "Decision: sessions are revoked on password change and on role removal.",
    ]),
    ("claude-code", "Fix flaky checkout test", "~/code/web-app", 2.2, [
        "The checkout e2e test fails about one run in ten.",
        "It raced the payment iframe; now it waits for the `ready` message.",
    ]),
    ("codex", "Migrate CI to GitHub Actions", "~/code/api-server", 3.1, [
        "Port the CI pipeline from CircleCI to GitHub Actions.",
        "Created .github/workflows/ci.yml with lint, type-check and tests on 3.11-3.13.",
    ]),
    ("gemini-cli", "Postgres connection pool sizing", "~/code/api-server", 4.6, [
        "What pool size for 4 web workers on a 2 vCPU Postgres?",
        "Start with 5 connections per worker (20 total) and watch for waits.",
    ]),
    ("cursor", "Dark mode for the dashboard", "~/code/web-app", 5.8, [
        "Add a dark mode toggle to the dashboard.",
        "Added CSS variables for both themes and a toggle that follows the system setting.",
    ]),
    ("claude-web", "Pricing page copy", None, 12, [
        "Help me write the pricing page for the hosted plan.",
        "Here is a draft with three tiers…",
    ]),
    ("chatgpt", "Queue choice: SQS or Redis streams", None, 16, [
        "For background jobs, SQS or Redis streams?",
        "SQS: you're on AWS already and don't want to run Redis just for this.",
    ]),
    ("gemini-web", "Explain OAuth PKCE", None, 23, [
        "Explain PKCE like I'm a backend developer.",
        "The client proves it started the login by revealing a secret it hashed earlier.",
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
        async with app.run_test(size=(124, 38)) as pilot:
            for _ in range(100):
                if app.thread_id is not None and not app.query_one("#prompt").disabled:
                    break
                await pilot.pause(0.05)
            await pilot.pause(0.3)
            app.save_screenshot(filename=out.name, path=str(out.parent))
            prompt = app.query_one("#prompt", Input)
            prompt.value = "session cookies"
            await pilot.press("enter")
            await pilot.pause(0.6)
            search = out.with_name("search.svg")
            app.save_screenshot(filename=search.name, path=str(search.parent))
        await service.close()
    print(f"wrote {out} and {out.with_name('search.svg')}")


if __name__ == "__main__":
    target = ROOT / "docs" / "screenshot.svg"
    target.parent.mkdir(exist_ok=True)
    asyncio.run(main(target))
