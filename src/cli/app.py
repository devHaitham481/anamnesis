"""NexusAI Textual TUI: thread sidebar, streaming chat canvas, model switching, slash commands.

All data access goes through `ChatService`; this module only handles presentation and input.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, Label, ListView, Select, Static

from src.cli.commands import Command, CommandError, help_text, is_command, parse_command
from src.cli.widgets import MessageView, ThreadItem
from src.config import Settings, load_settings
from src.core.chat import AssistantSaved, ChatService, Delta, UserSaved
from src.core.db import ThreadNotFoundError
from src.core.models import Message, Thread
from src.providers.router import ProviderError

log = logging.getLogger(__name__)

MAX_RENDERED_MESSAGES = 200
DELETE_CONFIRM_SECONDS = 10.0
EMPTY_HINT = (
    "Start typing to begin a new conversation.\n\n"
    "Ctrl+N new thread · Ctrl+T switch model · /help for commands"
)


class NexusApp(App[None]):
    TITLE = "NexusAI"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("ctrl+n", "new_thread", "New thread"),
        Binding("ctrl+t", "toggle_model", "Toggle model"),
        Binding("ctrl+l", "focus_threads", "Threads"),
        Binding("escape", "focus_prompt", "Prompt", show=False),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, settings: Settings | None = None, service: ChatService | None = None):
        super().__init__()
        self.settings = settings or load_settings()
        self.service: ChatService | None = service
        self._owns_service = service is None
        self.thread_id: str | None = None
        self.model: str = self.settings.default_model
        self._thread_model: str | None = None
        self._displayed: set[int] = set()
        self._cursor = 0
        self._threads_sig: tuple[int, float, int] | None = None
        self._streaming: str | None = None
        self._polling = False
        self._listing: list[str] = []
        self._delete_armed_at = 0.0

    # ---------- layout ----------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Label("THREADS", id="sidebar-title")
                yield ListView(id="threads")
            with Vertical(id="main"):
                with Horizontal(id="toolbar"):
                    yield Select[str]([], id="model", allow_blank=True, prompt="model")
                    yield Static("Loading memory…", id="status")
                yield VerticalScroll(id="chat")
                yield Input(placeholder="Loading…", id="prompt", disabled=True)
        yield Footer()

    @property
    def chat(self) -> VerticalScroll:
        return self.query_one("#chat", VerticalScroll)

    @property
    def svc(self) -> ChatService:
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
                self.service = await ChatService.create(self.settings)
            except Exception as e:
                log.exception("startup failed")
                self.query_one("#status", Static).update(f"Startup failed: {e}")
                return
        svc = self.svc
        if self.model not in svc.models:
            self.model = svc.default_model
        select = self.query_one("#model", Select)
        select.set_options([(m, m) for m in svc.models])
        with select.prevent(Select.Changed):
            select.value = self.model
        self._update_status()

        threads = await self._refresh_threads()
        if threads:
            await self._open_thread(threads[0].id)
        else:
            self._show_empty()
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.placeholder = "Message NexusAI…  (/help for commands)"
        prompt.focus()
        self.set_interval(self.settings.sync_interval, self._poll)

    async def on_unmount(self) -> None:
        if self._owns_service and self.service is not None:
            await self.service.close()

    # ---------- status / helpers ----------

    def _update_status(self) -> None:
        status = self.query_one("#status", Static)
        if self.service is None:
            return
        missing = self.svc.missing_key(self.model)
        memory = "semantic + keyword memory" if self.svc.index else "keyword memory only"
        if missing:
            status.update(f"⚠ {missing} not set in .env — {self.model} unavailable")
            status.add_class("warning")
        else:
            status.update(f"{self.model} · {memory}")
            status.remove_class("warning")

    def _show_empty(self) -> None:
        self.chat.remove_children()
        self.chat.mount(Static(EMPTY_HINT, id="empty"))
        self.sub_title = ""

    async def _mount(self, view: MessageView) -> None:
        for empty in self.chat.query("#empty"):
            await empty.remove()
        await self.chat.mount(view)
        self.chat.scroll_end(animate=False)

    async def _mount_message(self, msg: Message) -> None:
        self._displayed.add(msg.id)
        await self._mount(MessageView.from_message(msg))

    async def _note(self, markdown: str, kind: str = "note") -> None:
        await self._mount(MessageView(kind, "NexusAI" if kind == "note" else "Error", markdown))

    # ---------- threads ----------

    async def _refresh_threads(self) -> list[Thread]:
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
                    self.sub_title = t.title
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
        self._thread_model = thread.model
        self.sub_title = thread.title
        self._cursor = max(self._cursor, await svc.latest_message_id())
        msgs = await svc.messages(thread.id, limit=MAX_RENDERED_MESSAGES)
        await self.chat.remove_children()
        self._displayed = set()
        if len(msgs) == MAX_RENDERED_MESSAGES:
            await self._note(f"_Showing the last {MAX_RENDERED_MESSAGES} messages._")
        await self.chat.mount_all(MessageView.from_message(m) for m in msgs)
        self._displayed.update(m.id for m in msgs)
        if not msgs:
            self.chat.mount(Static(EMPTY_HINT, id="empty"))
        self.chat.scroll_end(animate=False)
        if thread.model in svc.models:
            self._set_model(thread.model)
        self._mark_current()

    def _set_model(self, model: str) -> None:
        self.model = model
        select = self.query_one("#model", Select)
        if select.value != model:
            with select.prevent(Select.Changed):
                select.value = model
        self._update_status()

    # ---------- sync with other clients (web UI, other terminals) ----------

    async def _poll(self) -> None:
        if self.service is None or self._polling:
            return
        self._polling = True
        try:
            if await self.svc.threads_signature() != self._threads_sig:
                await self._refresh_threads()
            await self._sync_messages()
        except Exception:
            log.exception("sync failed")
        finally:
            self._polling = False

    async def _sync_messages(self) -> None:
        if self.thread_id is None or self._streaming == self.thread_id:
            return
        for msg in await self.svc.messages_after(self._cursor):
            self._cursor = max(self._cursor, msg.id)
            if msg.thread_id == self.thread_id and msg.id not in self._displayed:
                await self._mount_message(msg)

    # ---------- input ----------

    @on(Input.Submitted, "#prompt")
    async def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or self.service is None:
            return
        event.input.value = ""
        if is_command(text):
            try:
                await self._run_command(parse_command(text))
            except CommandError as e:
                await self._note(str(e), "error")
            return
        if self._streaming is not None:
            self.notify("Still answering the previous message…", severity="warning")
            event.input.value = text
            return
        missing = self.svc.missing_key(self.model)
        if missing:
            await self._note(
                f"`{missing}` is not set. Add it to `.env` and restart, or switch model with "
                "`/model` / Ctrl+T.",
                "error",
            )
            event.input.value = text
            return
        self._send(text)

    @work(exclusive=False, group="send")
    async def _send(self, text: str) -> None:
        svc = self.svc
        if self.thread_id is None:
            thread = await svc.new_thread(model=self.model)
            self.thread_id = thread.id
            self._thread_model = thread.model
            self._displayed = set()
            self._cursor = max(self._cursor, await svc.latest_message_id())
            await self._refresh_threads()
        thread_id, model = self.thread_id, self.model
        self._streaming = thread_id
        view: MessageView | None = None
        streamed = ""
        try:
            async for ev in svc.send(thread_id, text, model):
                visible = self.thread_id == thread_id
                if isinstance(ev, UserSaved):
                    self._displayed.add(ev.message.id)
                    if visible:
                        await self._mount(MessageView.from_message(ev.message))
                elif isinstance(ev, Delta):
                    streamed += ev.text
                    if not visible:
                        continue
                    if view is None or not view.is_attached:
                        view = MessageView("assistant", f"{model} · streaming…")
                        await self._mount(view)
                        await view.write(streamed)
                    else:
                        await view.write(ev.text)
                    self.chat.scroll_end(animate=False)
                elif isinstance(ev, AssistantSaved):
                    self._displayed.add(ev.message.id)
                    self._thread_model = model
                    if view is not None and view.is_attached:
                        await view.finish()
                        view.set_header(MessageView.from_message(ev.message)._header)
                    elif self.thread_id == thread_id:
                        await self._mount(MessageView.from_message(ev.message))
        except Exception as e:
            if isinstance(e, ProviderError):
                log.warning("provider request failed: %s", e)
                text = str(e)
            else:
                log.exception("send failed")
                text = f"{type(e).__name__}: {e}"
            if view is not None and view.is_attached:
                await view.finish()
            if self.thread_id == thread_id:
                await self._note(text, "error")
            else:
                self.notify(f"Request failed: {e}", severity="error")
        finally:
            self._streaming = None
        # Not in `finally`: on cancellation (e.g. Ctrl+Q mid-stream) the DOM is being torn
        # down, so touching widgets would crash the shutdown. The partial reply is already saved.
        if self.is_running:
            await self._refresh_threads()
            await self._sync_messages()

    # ---------- commands ----------

    async def _run_command(self, cmd: Command) -> None:
        svc = self.svc
        if cmd.name == "help":
            await self._note(help_text())
        elif cmd.name == "quit":
            self.exit()
        elif cmd.name == "new":
            await self.action_new_thread(cmd.arg or None)
        elif cmd.name == "model":
            if not cmd.arg:
                await self._note(
                    f"Current model: **{self.model}**. Available: "
                    + ", ".join(f"`{m}`" for m in svc.models)
                )
                return
            if cmd.arg not in svc.models:
                raise CommandError(f"Unknown model {cmd.arg!r}. Available: {', '.join(svc.models)}")
            await self._switch_model(cmd.arg)
            await self._note(f"Switched to **{cmd.arg}**.")
        elif cmd.name == "history":
            threads = await svc.list_threads()
            self._listing = [t.id for t in threads[:30]]
            if not threads:
                await self._note("No threads yet.")
                return
            lines = [
                f"{i}. **{t.title}** — {t.model} · "
                f"{datetime.fromtimestamp(t.updated_at):%Y-%m-%d %H:%M}"
                for i, t in enumerate(threads[:30], 1)
            ]
            await self._note("**Recent threads** (open with `/open N`)\n\n" + "\n".join(lines))
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
                lines.append(f"{i}. **{h.thread_title}** ({h.role}): {snippet}")
            await self._note(f"**Search: {cmd.arg}** (open with `/open N`)\n\n" + "\n".join(lines))
        elif cmd.name == "open":
            n = int(cmd.arg)
            if not 1 <= n <= len(self._listing):
                raise CommandError("No such item — run /history or /search first.")
            await self._open_thread(self._listing[n - 1])
        elif cmd.name == "rename":
            if self.thread_id is None:
                raise CommandError("No active thread.")
            await svc.rename_thread(self.thread_id, cmd.arg)
            await self._refresh_threads()
        elif cmd.name == "delete":
            if self.thread_id is None:
                raise CommandError("No active thread.")
            if self._streaming == self.thread_id:
                raise CommandError("Wait for the current response to finish.")
            if time.monotonic() - self._delete_armed_at > DELETE_CONFIRM_SECONDS:
                self._delete_armed_at = time.monotonic()
                await self._note(
                    "Run `/delete` again within 10s to permanently delete this thread."
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
            self.notify("Thread deleted.")

    async def _switch_model(self, model: str) -> None:
        self._set_model(model)
        if self.thread_id is not None and self._thread_model != model:
            await self.svc.set_thread_model(self.thread_id, model)
            self._thread_model = model

    # ---------- events & actions ----------

    @on(ListView.Selected, "#threads")
    async def _thread_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ThreadItem) and event.item.thread_id != self.thread_id:
            await self._open_thread(event.item.thread_id)
        self.query_one("#prompt", Input).focus()

    @on(Select.Changed, "#model")
    async def _model_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str) and event.value != self.model:
            await self._switch_model(event.value)

    async def action_new_thread(self, title: str | None = None) -> None:
        if self.service is None:
            return
        thread = await self.svc.new_thread(model=self.model, title=title or "New chat")
        self.thread_id = None
        await self._refresh_threads()
        await self._open_thread(thread.id)
        self.query_one("#prompt", Input).focus()

    async def action_toggle_model(self) -> None:
        if self.service is None:
            return
        models = self.svc.models
        nxt = (
            models[(models.index(self.model) + 1) % len(models)]
            if self.model in models
            else models[0]
        )
        await self._switch_model(nxt)
        self.notify(f"Model: {nxt}")

    def action_focus_threads(self) -> None:
        self.query_one("#threads", ListView).focus()

    def action_focus_prompt(self) -> None:
        self.query_one("#prompt", Input).focus()
