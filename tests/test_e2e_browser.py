"""End-to-end: real uvicorn server + headless Chromium driving the web UI.

Skipped automatically when Playwright or its Chromium build is not installed
(`pip install -e ".[e2e]" && playwright install chromium`).
"""

from __future__ import annotations

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
from src.core.chat import ChatService
from src.core.db import Database
from src.core.vector import HashEmbedder, VectorIndex
from src.providers.router import ProviderRouter
from src.web.main import create_app
from tests.conftest import FakeLLM

sync_api = pytest.importorskip("playwright.sync_api")

REPLY = (
    "Here is **bold** text and code:\n\n```python\nprint('hi')\n```\n\n<img src=x onerror=alert(1)>"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[str, Settings, FakeLLM]]:
    settings = Settings(
        anthropic_api_key="fake",  # type: ignore[arg-type]
        gemini_api_key=None,
        data_dir=tmp_path / "data",
        sync_interval=0.2,
    )
    llm = FakeLLM()
    llm.reply = REPLY

    async def factory(s: Settings) -> ChatService:
        db = await Database(s.db_path).open()
        idx = VectorIndex(s.vector_path, HashEmbedder())
        return ChatService(db, ProviderRouter(s, completion_fn=llm), idx, s)

    port = _free_port()
    config = uvicorn.Config(
        create_app(settings, factory), host="127.0.0.1", port=port, log_level="warning"
    )
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", settings, llm
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


def test_full_web_flow(server: tuple[str, Settings, FakeLLM], browser: object) -> None:
    url, settings, llm = server
    expect = sync_api.expect
    errors: list[str] = []
    dialogs: list[str] = []

    page = browser.new_page()  # type: ignore[attr-defined]
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    page.goto(url)

    expect(page.locator("#conn-text")).to_have_text("Live")
    expect(page.locator("#empty")).to_be_visible()
    assert page.locator("#model option").all_inner_texts() == ["claude", "gemini (no key)"]

    # Mirror page opened before the chat starts.
    mirror = browser.new_page()  # type: ignore[attr-defined]
    mirror.goto(url)
    expect(mirror.locator("#conn-text")).to_have_text("Live")

    # --- send a message: streams, renders markdown safely, creates a titled thread
    page.fill("#input", "Explain the storage layer")
    page.press("#input", "Enter")
    expect(page.locator("article.msg.user .msg-body")).to_have_text("Explain the storage layer")
    assistant = page.locator("article.msg.assistant[data-id]")
    expect(assistant).to_have_count(1)
    expect(assistant.locator("strong")).to_have_text("bold")
    expect(assistant.locator("pre code")).to_have_text("print('hi')")
    expect(assistant.locator("img")).to_have_count(0)  # raw HTML is escaped
    expect(assistant).to_contain_text("<img src=x onerror=alert(1)>")
    expect(page.locator("article.msg.streaming")).to_have_count(0)
    expect(page.locator(".thread.active .thread-title")).to_have_text("Explain the storage layer")
    expect(page.locator("#thread-title")).to_have_text("Explain the storage layer")
    expect(page.locator("#input")).to_have_value("")

    # --- mirror tab sees the new thread; opening it shows the conversation
    expect(mirror.locator(".thread .thread-title")).to_have_text(["Explain the storage layer"])
    mirror.locator(".thread").first.click()
    expect(mirror.locator("article.msg[data-id]")).to_have_count(2)

    # --- a second turn streams live into the mirror too (no duplicates)
    page.fill("#input", "And the vector index?")
    page.press("#input", "Enter")
    expect(mirror.locator("article.msg[data-id]")).to_have_count(4)
    expect(page.locator("article.msg[data-id]")).to_have_count(4)

    # --- write from another process (the TUI) appears without reload
    thread_id = page.evaluate("location.hash.slice(1)")

    # Separate OS process, like the TUI.
    script = (
        "import asyncio, sys; from src.core.db import Database\n"
        "async def main():\n"
        "    async with Database(sys.argv[1]) as db:\n"
        "        await db.add_message(sys.argv[2], 'user', 'typed in the terminal')\n"
        "asyncio.run(main())"
    )
    subprocess.run(
        [sys.executable, "-c", script, str(settings.db_path), thread_id], check=True, timeout=30
    )
    expect(page.locator("article.msg.user .msg-body").last).to_have_text("typed in the terminal")
    expect(mirror.locator("article.msg[data-id]")).to_have_count(5)

    # --- model without key shows banner and blocks sending
    page.select_option("#model", "gemini")
    expect(page.locator("#banner")).to_contain_text("GEMINI_API_KEY")
    page.fill("#input", "should not send")
    page.press("#input", "Enter")
    page.wait_for_timeout(300)
    expect(page.locator("#input")).to_have_value("should not send")
    page.select_option("#model", "claude")
    expect(page.locator("#banner")).to_be_hidden()

    # --- provider error is surfaced
    llm.fail = RuntimeError("upstream overloaded")
    page.fill("#input", "this will fail")
    page.press("#input", "Enter")
    expect(page.locator("article.msg.error")).to_contain_text("upstream overloaded")
    expect(page.locator("#send")).to_be_enabled()
    llm.fail = None

    # --- search and open result
    # Sending immediately after "+ New" must land in the new thread (race regression).
    page.click("#new-thread")
    page.fill("#input", "quick follow-up in a fresh thread")
    page.press("#input", "Enter")
    expect(page.locator(".thread")).to_have_count(2)
    expect(page.locator("#thread-title")).to_have_text("quick follow-up in a fresh thread")
    expect(page.locator("article.msg[data-id]")).to_have_count(2)
    page.fill("#search", "vector index")
    page.press("#search", "Enter")
    expect(page.locator("#search-list .hit").first).to_contain_text("Explain the storage layer")
    page.locator("#search-list .hit").first.click()
    expect(page.locator("#thread-title")).to_have_text("Explain the storage layer")

    # --- deep link survives reload
    page.reload()
    expect(page.locator("#thread-title")).to_have_text("Explain the storage layer")
    expect(page.locator("article.msg[data-id]")).to_have_count(
        6
    )  # 2 turns + terminal msg + failed turn (user only)

    # --- delete thread (confirm dialog) syncs to mirror
    page.locator(".thread.active").hover()
    page.locator(".thread.active .thread-del").click()
    expect(page.locator(".thread")).to_have_count(1)
    expect(mirror.locator(".thread")).to_have_count(1)
    expect(mirror.locator(".thread-title")).to_have_text(["quick follow-up in a fresh thread"])
    assert dialogs and "Delete" in dialogs[0]

    # --- mobile layout: sidebar hidden behind toggle, no horizontal scroll
    page.set_viewport_size({"width": 390, "height": 800})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    expect(page.locator("#toggle-sidebar")).to_be_visible()

    assert errors == [], errors
