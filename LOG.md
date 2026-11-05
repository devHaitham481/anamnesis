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
