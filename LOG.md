# LOG.md - Implementation Log

## 2026-11-05 — Sprint 1: Setup & Core Gateway

- **Project setup:** `pyproject.toml` (Python ≥3.11, litellm, pydantic, python-dotenv; dev: pytest, pytest-asyncio, ruff, mypy). Package skeleton `src/{core,providers,cli,web}`, `tests/`, `.gitignore`, `.env.example`. Dev env: `uv venv -p 3.13 .venv && uv pip install -e ".[dev]"` (system Python is 3.9).
- **Config (`src/config.py`):** `load_settings()` merges `.env` with the process environment (env wins); keys stored as `SecretStr`; `Settings.require_key()` raises `MissingAPIKeyError`. Model defaults overridable via `NEXUS_CLAUDE_MODEL` / `NEXUS_GEMINI_MODEL`.
- **Router (`src/providers/router.py`):** `ProviderRouter` (alias `ProviderManager`) resolves `claude`/`gemini` aliases or `anthropic/…`/`gemini/…` strings, injects the key, and normalizes LiteLLM output into `CompletionResult` / `StreamChunk` (`src/providers/models.py`). Completion function is injectable for testing.
- **Tests:** 16 unit tests (config + router via LiteLLM `mock_response`, no network). 4 live integration tests for Claude & Gemini (complete + stream) are **skipped** — no `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` in the environment. Add them to `.env` and run `pytest -m integration`.
- **Open item:** default model IDs (`anthropic/claude-sonnet-5-5`, `gemini/gemini-2.5-flash`) are unverified against live APIs until keys are provided.

## 2026-11-05 — Sprint 2: Local Memory & Context

- **`src/core/db.py`:** async SQLite (`aiosqlite`, WAL, FK cascade) with `providers`, `threads`, `messages` tables + FTS5 keyword index kept in sync by triggers. Monotonic message ids enable cross-process change polling (`messages_after`, `threads_signature`).
- **`src/core/vector.py`:** LanceDB index (cosine) keyed by message id. Default embedder is local `fastembed` (`BAAI/bge-small-en-v1.5`, ~10 ms/embed, cached under `<data_dir>/models`); `HashEmbedder` for tests. Auto-rebuilds if embedding dimension changes.
- **`src/core/context.py`:** `assemble_prompt()` = one system message (prompt + retrieved snippets) + recent history within a char budget, always starting on a user turn.
- **`src/core/chat.py`:** `ChatService` — single orchestrator for TUI and web: persists user msg → retrieves cross-thread snippets (excluding in-window history, min cosine 0.6, calibrated on bge-small) → streams via router → persists assistant msg (partial + note if interrupted) → indexes. Degrades gracefully to keyword-only memory if the embedder can't load; `backfill_index()` catches up on start.
- **Tests:** 26 new (db, vector, context, chat) incl. <50 ms latency checks for DB lookups and vector search.

## 2026-11-05 — Sprint 3: Terminal UI

- **`src/cli/app.py`** (`python -m src.cli` / `nexus`): Textual app with thread sidebar, model `Select` + `Ctrl+T` toggle, streaming Markdown chat canvas (`Markdown.get_stream`), `Ctrl+N` new thread. Talks only to `ChatService` (no direct DB access). Logs to `<data_dir>/nexus.log` so nothing corrupts the terminal.
- **Slash commands** (`src/cli/commands.py`): `/help /new /model /history /search /open N /rename /delete (confirm) /quit`.
- **Live sync:** polls the service change feed (message ids + thread signature) every second so messages/threads written by the web UI or another terminal appear automatically; handles deletion of the open thread elsewhere.
- **Robustness:** missing API key → inline guidance, input preserved, no request sent; provider errors shown inline; switching threads mid-stream keeps the stream persisting.
- **Bug caught by tests:** `MarkdownStream.write` is a coroutine — un-awaited it rendered nothing. Fixed, and pytest now treats `RuntimeWarning` as an error. Verified visually via SVG screenshot.
- **Tests:** 11 pilot-driven TUI tests + 8 command-parser tests.

## 2026-11-05 — Sprint 4: Web Dashboard & Sync

- **`src/web/main.py`** (`python -m src.web` / `uvicorn src.web.main:app`): REST (`/api/health`, `/api/models`, `/api/threads` CRUD, `/api/threads/{id}/messages`, `/api/search`), SSE turn streaming (`POST /api/threads/{id}/chat`), WebSocket `/ws` (send/ping; broadcasts `stream_start`/`delta`/`message`/`stream_end`/`threads_changed` to every browser).
- **Cross-process sync:** a watcher polls the SQLite change feed and pushes writes from the TUI (or any process) to browsers; clients dedupe by message id.
- **Security (local-first):** binds 127.0.0.1 by default; `TrustedHostMiddleware` (DNS-rebinding), WebSocket Origin check (cross-site WS hijacking), optional `NEXUS_AUTH_TOKEN` bearer auth for LAN/multi-device use.
- **Frontend** (`src/web/static`, no build step): threads sidebar, live streaming, model select with missing-key banner, search, rename (double-click title), delete, deep links, reconnect with catch-up, light/dark, mobile drawer. Markdown renderer escapes all HTML first.
- **Bugs caught by e2e (Playwright) test:** (1) null deref dropped the user bubble when a stream had started; (2) sending right after "+ New" went to the previous thread. Both fixed with regression coverage.
- **Validated for real:** `python -m src.web` with real fastembed embeddings + curl smoke test; `python -m src.cli` launched in a pty against the same data dir.

## 2026-11-05 — Independent review fixes

An independent review agent audited the codebase; all findings fixed with regression tests (`tests/test_review_regressions.py`):

1. **SQLite lock leak (confirmed):** a failed/cancelled write left sqlite3's implicit transaction open, holding the WAL write lock and blocking the other process. All writes now go through `Database._write()` (commit or always-rollback).
2. **LanceDB cross-process staleness + duplicates (confirmed):** connect with `read_consistency_interval=0`; `add` is an upsert (`merge_insert` on `message_id`); search dedupes.
3. **TUI crash on Ctrl+Q mid-stream (confirmed):** UI refresh no longer runs in the cancelled worker's `finally`. Mutation-tested: the regression test fails without the fix.
4. **Interleaved concurrent turns on one thread:** `ChatService` allows one turn per thread (`ThreadBusyError`, atomic check-and-claim). Web: SSE → 409; WS → error to the sender only; a rejected turn broadcasts nothing.
5. **WS reconnect unlocked Send mid-stream:** server sends `{"type":"active","thread_ids":[...]}` on connect; client keeps threads busy across disconnects and reconciles on reconnect.
- Lower priority: provider HTTP stream is closed promptly on cancel (`aclosing` + `aclose`), broadcasts are concurrent with a per-socket timeout, stale `loadThreads` responses are ignored, partial-save tolerates a thread deleted mid-stream.
- **Known limitation:** the per-thread lock is per process; simultaneous sends to the *same* thread from TUI and web at the same instant are not serialized across processes.
- Tests: 78 passed, 4 skipped (live provider tests — no API keys in this environment).

## 2026-11-05 — First live run with real API keys

- **Gemini:** `gemini-2.5-flash` was retired for new users (404), so the default is now `gemini/gemini-3.8-flash`, the model Google recommends. During testing 3.8-flash often returned 503 "high demand", so the full live run used `gemini-3.5-flash`. Web server → SSE → DB → assistant reply worked, and a *new* thread correctly recalled a fact from another thread via RAG.
- **Claude:** the key authenticates, but the account has no API credit (400 "credit balance is too low"). This needs action on the account side.
- **Hardening from the live run:**
  - Provider requests now have a bounded `timeout` (`NEXUS_REQUEST_TIMEOUT`, default 120 s) and `num_retries` (`NEXUS_MAX_RETRIES`, default 2). An overloaded provider previously hung a turn for minutes.
  - LiteLLM exceptions (before or mid-stream) become a one-line `ProviderError`, e.g. "Gemini is temporarily unavailable: …", "Claude request failed: Your credit balance is too low…". TUI and web show it without a class name or JSON dump and log it as a warning, without a traceback.
  - `pytest` now excludes live tests by default (they cost money and depend on provider uptime); run them with `pytest -m integration`.
- Tests: 90 passed (+12 router error tests). Ruff and mypy clean.

## 2026-11-06 — Sprint 5: shared memory across all AI tools

**Direction change (user):** NexusAI is meant to be one memory underneath every AI tool the user already uses, not a gateway where you pick a model. The chat gateway is kept as a side feature.

- **Storage:** threads carry `source`, `project` and `external_id`; messages carry `external_id`. Old databases migrate in place. `import_conversation` is idempotent through unique indexes, so re-scanning a grown transcript adds only the tail.
- **Importers** (`src/ingest`):
  - Claude Code: verified against the real `~/.claude/projects` (44 sessions, ~6k messages).
  - Codex, Gemini CLI, Cursor: built from their current source/format docs. These tools are not installed here, so they are covered by fixture tests only.
  - Web exports: ChatGPT (the visible branch only), claude.ai, Gemini Takeout (grouped into sessions).
  - Plumbing (tool calls and results, thinking, injected context, task notifications, sidechains) is filtered out.
- **MCP server** (`nexus mcp` / `python -m src.memory_mcp`, MCP SDK 2.3): tested end to end over real stdio from a foreign working directory. `.env` falls back to the project's copy when launched elsewhere.
- **`nexus connect`:** merges into Claude Desktop/Cursor/Gemini JSON configs and Codex TOML, with `.bak` backups and a refusal to touch invalid files. Claude Code is registered via `claude mcp add --scope user`.
- **Performance:** the first real sync took 11 minutes because embedding full-length messages runs ~10/s on this CPU. Fixes:
  - text is imported first (searchable immediately), and embedding runs separately in batches with progress;
  - only the first 1,000 chars are embedded;
  - messages under 20 chars are skipped.
  - A cross-process `flock` keeps several MCP servers (one per agent window) from syncing at once.
- **Search:** keyword search drops filler words. Previously "why did we pick X" matched every "why"/"we".
- **Flaky TUI test fixed (pre-existing, ~1/12):** the poller and a finished reply could rebuild the sidebar concurrently. Rebuilds are now serialized by a lock.
- Tests: 121 passed (3 runs). Ruff and mypy clean.

## 2026-11-11 — Memory across computers

**Goal (user):** any new computer, account or Claude Code instance automatically has the same memory. The user chose a synced folder (iCloud Drive by default) over a hosted relay for now.

- **`src/core/devices.py`:** every device writes only its own subfolder of immutable segments (`000001.nxs`, …), each one a gzipped JSONL of conversations, AES-256-GCM encrypted with the device id as associated data.
  - Other devices import the segments in order through the same idempotent upsert as local history, so a missing segment pauses that device's import rather than being skipped.
  - Messages record their `origin` device, so received memory is never echoed back.
  - Native threads get a stable cross-device `external_id`, so a chat continued on another computer merges into the same thread.
- **Pairing:** the key exists only in the pairing code and in `~/.nexus-ai/sync.json` (mode 600). `meta.json` holds an HMAC check, so a wrong code is rejected. The tests check that the key never appears in the folder.
- **Integration:** runs as part of every background sync (MCP server, web server, TUI) and `nexus sync`. iCloud placeholders are downloaded via `brctl`.
- **Bug found by tests:** a corrupted later segment hid the messages already imported from earlier segments in the report. Progress is now reported per segment, alongside the error.
- **Verified:**
  - Unit tests cover two- and three-device flows, merges, echo-freedom, gaps, tampering and an unreachable folder.
  - An end-to-end CLI run with two data dirs: share, then join, then search on the second computer.
  - Tests: 126 passed (3 runs).
- **Not yet:** deletions don't propagate across devices, and there is no hosted relay (the segment format is designed so one can be added without changes).

## 2026-11-11 — Renamed to Anamnesis; new landing page
- NexusAI is now **Anamnesis** (Plato: learning is recollection). CLI `anam` (`anam-web`, `anam-mcp`), package `anamnesis`, MCP server `anamnesis`, data in `~/.anamnesis`.
- Backward compatible: `~/.nexus-ai` (and `nexus.db`) is moved on first run; `NEXUS_*` env vars still work as fallbacks; native chats stored with source `nexus` are relabelled `anamnesis`.
- `site/index.html` rebuilt after a REA capture of exe.dev: left rail, paper sheets, serif + typewriter type, live terminal demos with replay/tabs, Greek-key bands and a terracotta/black-figure palette.
