"""End-to-end: real uvicorn server + headless Chromium driving the memory browser.

Skipped automatically when Playwright or its Chromium build is not installed
(`pip install -e ".[e2e]" && playwright install chromium`).
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from src.config import Settings
from src.core.db import Database
from src.web.main import create_app
from tests.conftest import conversation, open_service

sync_api = pytest.importorskip("playwright.sync_api")

REPLY = (
    "Here is **bold** text and code:\n\n```python\nprint('hi')\n```\n\n<img src=x onerror=alert(1)>"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _seed(settings: Settings) -> None:
    async with Database(settings.db_path) as db:
        await db.import_conversation(
            conversation(
                "s1",
                "Explain the storage layer",
                REPLY,
                title="Storage layer",
                project="/w/anamnesis",
                start=1_000,
            )
        )
        await db.import_conversation(
            conversation(
                "g1", "ideas for a lisbon trip", "go in spring", source="chatgpt", title="Lisbon"
            )
        )


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[str, Settings]]:
    settings = Settings(data_dir=tmp_path / "data", sync_interval=0.2, memory_sync=False)
    asyncio.run(_seed(settings))
    port = _free_port()
    config = uvicorn.Config(
        create_app(settings, open_service), host="127.0.0.1", port=port, log_level="warning"
    )
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", settings
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def browser() -> Iterator[object]:
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # browser binary not installed
            pytest.skip(f"Chromium unavailable: {e}")
        yield b
        b.close()


def test_full_web_flow(server: tuple[str, Settings], browser: object) -> None:
    url, settings = server
    expect = sync_api.expect
    errors: list[str] = []
    dialogs: list[str] = []

    page = browser.new_page()  # type: ignore[attr-defined]
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    page.goto(url)

    expect(page.locator("#conn-text")).to_have_text("Live")
    # No chat UI at all.
    expect(page.locator("#input")).to_have_count(0)
    expect(page.locator("#model")).to_have_count(0)
    expect(page.locator("#new-thread")).to_have_count(0)

    # --- the most recent conversation opens; markdown is rendered safely
    expect(page.locator(".thread .thread-title")).to_have_text(["Lisbon", "Storage layer"])
    expect(page.locator("#thread-title")).to_have_text("Lisbon")
    expect(page.locator("#thread-meta")).to_have_text("ChatGPT")
    page.locator(".thread", has_text="Storage layer").click()
    expect(page.locator("#thread-meta")).to_have_text("Claude Code · /w/anamnesis")
    assistant = page.locator("article.msg.assistant[data-id]")
    expect(assistant).to_have_count(1)
    expect(assistant.locator("strong")).to_have_text("bold")
    expect(assistant.locator("pre code")).to_have_text("print('hi')")
    expect(assistant.locator("img")).to_have_count(0)  # raw HTML is escaped
    expect(assistant).to_contain_text("<img src=x onerror=alert(1)>")

    # --- source filter
    page.select_option("#source-filter", "chatgpt")
    expect(page.locator(".thread .thread-title")).to_have_text(["Lisbon"])
    page.select_option("#source-filter", "")
    expect(page.locator(".thread")).to_have_count(2)

    # --- a mirror tab gets live updates when another process writes (background import)
    mirror = browser.new_page()  # type: ignore[attr-defined]
    mirror.goto(url + "#" + page.evaluate("location.hash.slice(1)"))
    expect(mirror.locator("article.msg[data-id]")).to_have_count(2)
    script = (
        "import asyncio, sys; from src.core.db import Database\n"
        "from tests.conftest import conversation\n"
        "async def main():\n"
        "    async with Database(sys.argv[1]) as db:\n"
        "        c = conversation('s1', 'Explain the storage layer', 'x',\n"
        "            'and what about the vector index?', title='Storage layer', start=1_000)\n"
        "        await db.import_conversation(c)\n"
        "asyncio.run(main())"
    )
    subprocess.run([sys.executable, "-c", script, str(settings.db_path)], check=True, timeout=30)
    for p in (page, mirror):
        expect(p.locator("article.msg.user .msg-body").last).to_have_text(
            "and what about the vector index?"
        )
        expect(p.locator("article.msg[data-id]")).to_have_count(3)

    # --- search and open result
    page.fill("#search", "vector index")
    page.press("#search", "Enter")
    expect(page.locator("#search-list .hit").first).to_contain_text("Storage layer")
    page.locator("#search-list .hit").first.click()
    expect(page.locator("#thread-title")).to_have_text("Storage layer")

    # --- deep link survives reload
    page.reload()
    expect(page.locator("#thread-title")).to_have_text("Storage layer")
    expect(page.locator("article.msg[data-id]")).to_have_count(3)

    # --- delete (confirm dialog) syncs to the mirror
    page.locator(".thread.active").hover()
    page.locator(".thread.active .thread-del").click()
    expect(page.locator(".thread")).to_have_count(1)
    expect(mirror.locator(".thread-title")).to_have_text(["Lisbon"])
    assert dialogs and "Delete" in dialogs[0]

    # --- mobile layout: sidebar hidden behind toggle, no horizontal scroll
    page.set_viewport_size({"width": 390, "height": 800})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    expect(page.locator("#toggle-sidebar")).to_be_visible()

    assert errors == [], errors
