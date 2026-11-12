"""Anamnesis Textual TUI: a browser for the shared memory.

Sidebar of conversations from every tool, a read-only conversation view, and one input line:
plain text searches all memory, slash commands browse and manage it. All data access goes
through `MemoryService`; this module only handles presentation and input.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, Label, ListView, Static

from anamnesis.cli.commands import Command, CommandError, help_text, is_command, parse_command
from anamnesis.cli.widgets import MessageView, ThreadItem
from anamnesis.config import Settings, load_settings
from anamnesis.core.db import ThreadNotFoundError
from anamnesis.core.memory import MemoryService
from anamnesis.core.models import SOURCE_LABELS, Message, Thread

log = logging.getLogger(__name__)

MAX_RENDERED_MESSAGES = 200
DELETE_CONFIRM_SECONDS = 10.0
EMPTY_HINT = (
    "Nothing in memory yet.\n\n"
    "Run /sync to import your Claude Code, Codex, Gemini CLI and Cursor history, or\n"
    "`anam import <export.zip>` for ChatGPT, claude.ai and Gemini exports.\n\n"
    "/help for commands"
)


class AnamnesisApp(App[None]):
    TITLE = "Anamnesis"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("ctrl+l", "focus_threads", "Conversations"),
        Binding("ctrl+f", "focus_prompt", "Search"),
        Binding("escape", "focus_prompt", "Search", show=False),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, settings: Settings | None = None, service: MemoryService | None = None):
        super().__init__()
        self.settings = settings or load_settings()
        self.service: MemoryService | None = service
        self._owns_service = service is None
        self.thread_id: str | None = None
        self._thread_source: str | None = None
        self._displayed: set[int] = set()
        self._cursor = 0
        self._threads_sig: tuple[int, float, int] | None = None
        # Serializes sidebar rebuilds: the poller and a sync can both trigger one.
        self._threads_lock = asyncio.Lock()
        self._polling = False
        self._listing: list[str] = []
        self._delete_armed_at = 0.0

    # ---------- layout ----------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Label("CONVERSATIONS", id="sidebar-title")
                yield ListView(id="threads")
            with Vertical(id="main"):
                yield Static("Loading memory…", id="status")
                yield VerticalScroll(id="view")
                yield Input(placeholder="Loading…", id="prompt", disabled=True)
        yield Footer()

    @property
    def view(self) -> VerticalScroll:
        return self.query_one("#view", VerticalScroll)

    @property
    def svc(self) -> MemoryService:
        if self.service is None:
            raise RuntimeError("Service not ready")
        return self.service

    # ---------- startup / shutdown ----------

    def on_mount(self) -> None:
        self._initialize()

    @work(exclusive=True, group="init")
    async def _initialize(self) -> None:
        if self.service is None:
            try:
                self.service = await MemoryService.create(self.settings)
            except Exception as e:
                log.exception("startup failed")
                self.query_one("#status", Static).update(f"Startup failed: {e}")
                return
        await self._update_status()

        threads = await self._refresh_threads()
        if threads:
            await self._open_thread(threads[0].id)
        else:
            self._show_empty()
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.placeholder = "Search all memory…  (/help for commands)"
        prompt.focus()
        self.set_interval(self.settings.sync_interval, self._poll)
        if self.settings.memory_sync:
            self._memory_sync_loop()

    @work(exclusive=True, group="memory-sync")
    async def _memory_sync_loop(self) -> None:
        """Keep importing other tools' history while the TUI is open (new threads appear live)."""
        from anamnesis.ingest.runner import watch

        await watch(self.svc, interval=self.settings.memory_sync_interval)

    @work(exclusive=True, group="memory-sync-now")
    async def _memory_sync_now(self) -> None:
        from anamnesis.ingest.runner import sync_once

        try:
            report = await sync_once(self.svc)
        except Exception as e:
            log.exception("manual sync failed")
            await self._note(f"Sync failed: {e}", "error")
            return
        if report.messages_added:
            per = ", ".join(f"{SOURCE_LABELS.get(k, k)}: {v}" for k, v in report.by_source.items())
            await self._note(f"Imported {report.messages_added} new messages ({per}).")
        else:
            await self._note("Memory is up to date (or another Anamnesis process is syncing).")
        if self.is_running:
            await self._refresh_threads()

    async def on_unmount(self) -> None:
        if self._owns_service and self.service is not None:
            await self.service.close()

    # ---------- status / helpers ----------

    async def _update_status(self) -> None:
        if self.service is None:
            return
        counts = await self.svc.source_counts()
        mode = "semantic + keyword search" if self.svc.index else "keyword search only"
        self.query_one("#status", Static).update(
            f"{sum(counts.values())} conversations from {len(counts)} sources · {mode}"
        )

    def _show_empty(self) -> None:
        self.view.remove_children()
        self.view.mount(Static(EMPTY_HINT, id="empty"))
        self.sub_title = ""

    async def _mount(self, view: MessageView) -> None:
        for empty in self.view.query("#empty"):
            await empty.remove()
        await self.view.mount(view)
        self.view.scroll_end(animate=False)

    async def _mount_message(self, msg: Message, source: str | None = None) -> None:
        self._displayed.add(msg.id)
        await self._mount(MessageView.from_message(msg, source))

    async def _note(self, markdown: str, kind: str = "note") -> None:
        await self._mount(MessageView(kind, "Anamnesis" if kind == "note" else "Error", markdown))

    # ---------- threads ----------

    async def _refresh_threads(self) -> list[Thread]:
        async with self._threads_lock:
            return await self._rebuild_threads()

    async def _rebuild_threads(self) -> list[Thread]:
        svc = self.svc
        self._threads_sig = await svc.threads_signature()
        threads = await svc.list_threads()
        lv = self.query_one("#threads", ListView)
        await lv.clear()
        await lv.extend(ThreadItem(t) for t in threads)
        self._mark_current()
        if self.thread_id is not None and all(t.id != self.thread_id for t in threads):
            # Current thread was deleted (possibly from another client).
            self.thread_id = None
            self._show_empty()
        else:
            for t in threads:
                if t.id == self.thread_id:
                    self.sub_title = _subtitle(t)
        await self._update_status()
        return threads

    def _mark_current(self) -> None:
        lv = self.query_one("#threads", ListView)
        for i, item in enumerate(lv.query(ThreadItem)):
            current = item.thread_id == self.thread_id
            item.set_class(current, "current")
            if current:
                lv.index = i

    async def _open_thread(self, thread_id: str) -> None:
        svc = self.svc
        try:
            thread = await svc.get_thread(thread_id)
        except ThreadNotFoundError:
            await self._refresh_threads()
            return
        self.thread_id = thread.id
        self._thread_source = thread.source
        self.sub_title = _subtitle(thread)
        self._mark_current()  # highlight immediately, before the (slower) message render
        self._cursor = max(self._cursor, await svc.latest_message_id())
        msgs = await svc.messages(thread.id, limit=MAX_RENDERED_MESSAGES)
        await self.view.remove_children()
        self._displayed = set()
        if len(msgs) == MAX_RENDERED_MESSAGES:
            await self._note(f"_Showing the last {MAX_RENDERED_MESSAGES} messages._")
        await self.view.mount_all(MessageView.from_message(m, thread.source) for m in msgs)
        self._displayed.update(m.id for m in msgs)
        if not msgs:
            self.view.mount(Static("(no messages)", id="empty"))
        self.view.scroll_end(animate=False)

    # ---------- live updates (background imports, other devices, the web UI) ----------

    async def _poll(self) -> None:
        if self.service is None or self._polling:
            return
        self._polling = True
        try:
            if await self.svc.threads_signature() != self._threads_sig:
                await self._refresh_threads()
            await self._sync_messages()
        except Exception:
            log.exception("refresh failed")
        finally:
            self._polling = False

    async def _sync_messages(self) -> None:
        if self.thread_id is None:
            return
        for msg in await self.svc.messages_after(self._cursor):
            self._cursor = max(self._cursor, msg.id)
            if msg.thread_id == self.thread_id and msg.id not in self._displayed:
                await self._mount_message(msg, self._thread_source)

    # ---------- input ----------

    @on(Input.Submitted, "#prompt")
    async def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or self.service is None:
            return
        event.input.value = ""
        try:
            cmd = parse_command(text) if is_command(text) else Command("search", text)
            await self._run_command(cmd)
        except CommandError as e:
            await self._note(str(e), "error")

    # ---------- commands ----------

    async def _run_command(self, cmd: Command) -> None:
        svc = self.svc
        if cmd.name == "help":
            await self._note(help_text())
        elif cmd.name == "quit":
            self.exit()
        elif cmd.name == "history":
            source = cmd.arg.strip().lower() or None
            threads = await svc.list_threads(limit=30, source=source)
            self._listing = [t.id for t in threads]
            if not threads:
                await self._note("No conversations" + (f" from `{source}`." if source else " yet."))
                return
            lines = [
                f"{i}. **{t.title}** — {SOURCE_LABELS.get(t.source, t.source)} · "
                f"{datetime.fromtimestamp(t.updated_at):%Y-%m-%d %H:%M}"
                for i, t in enumerate(threads, 1)
            ]
            await self._note(
                "**Recent conversations** (open with `/open N`)\n\n" + "\n".join(lines)
            )
        elif cmd.name == "sync":
            await self._note("Importing new history from your AI tools…")
            self._memory_sync_now()
        elif cmd.name == "search":
            hits = await svc.search(cmd.arg, k=10)
            self._listing = [h.thread_id for h in hits]
            if not hits:
                await self._note(f"No results for _{cmd.arg}_.")
                return
            lines = []
            for i, h in enumerate(hits, 1):
                snippet = " ".join(h.content.split())
                snippet = snippet if len(snippet) <= 160 else snippet[:159] + "…"
                where = SOURCE_LABELS.get(h.source or "", h.source or "")
                lines.append(f"{i}. **{h.thread_title}** · {where} ({h.role}): {snippet}")
            await self._note(f"**Search: {cmd.arg}** (open with `/open N`)\n\n" + "\n".join(lines))
        elif cmd.name == "open":
            n = int(cmd.arg)
            if not 1 <= n <= len(self._listing):
                raise CommandError("No such item — search or run /history first.")
            await self._open_thread(self._listing[n - 1])
        elif cmd.name == "rename":
            if self.thread_id is None:
                raise CommandError("No conversation open.")
            await svc.rename_thread(self.thread_id, cmd.arg)
            await self._refresh_threads()
        elif cmd.name == "delete":
            if self.thread_id is None:
                raise CommandError("No conversation open.")
            if time.monotonic() - self._delete_armed_at > DELETE_CONFIRM_SECONDS:
                self._delete_armed_at = time.monotonic()
                await self._note(
                    "Run `/delete` again within 10s to permanently delete this conversation "
                    "from memory."
                )
                return
            self._delete_armed_at = 0.0
            await svc.delete_thread(self.thread_id)
            self.thread_id = None
            threads = await self._refresh_threads()
            if threads:
                await self._open_thread(threads[0].id)
            else:
                self._show_empty()
            self.notify("Conversation deleted.")

    # ---------- events & actions ----------

    @on(ListView.Selected, "#threads")
    async def _thread_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ThreadItem) and event.item.thread_id != self.thread_id:
            await self._open_thread(event.item.thread_id)

    def action_focus_threads(self) -> None:
        self.query_one("#threads", ListView).focus()

    def action_focus_prompt(self) -> None:
        self.query_one("#prompt", Input).focus()


def _subtitle(thread: Thread) -> str:
    label = SOURCE_LABELS.get(thread.source, thread.source)
    return f"{thread.title} · {label}" + (f" · {thread.project}" if thread.project else "")
