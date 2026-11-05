# TASKS.md - Progress Tracker

## Status Key

- [ ] Pending
- [/] In Progress
- [x] Completed

---

## Task Board

### Sprint 1: Setup & Core Gateway

- [x] Initialize project directory structure and dependencies.
- [x] Implement `.env` loader for `ANTHROPIC_API_KEY` and `GEMINI_API_KEY`.
- [x] Create `/src/providers/router.py` using `litellm` for standardized completions.
- [x] Write integration test `tests/test_router.py` checking Claude and Gemini API responses.

### Sprint 2: Local Memory & Context

- [x] Create SQLite database manager (`/src/core/db.py`) for thread logging.
- [x] Set up LanceDB embedding indexer (`/src/core/vector.py`).
- [x] Build RAG context-assembly function to inject history into new prompts.

### Sprint 3: Terminal UI (TUI)

- [x] Create Textual TUI app shell in `/src/cli/app.py`.
- [x] Implement active thread list sidebar and real-time streaming chat canvas.
- [x] Add model switching dropdown/hotkey (Claude ↔ Gemini).

### Sprint 4: Web Dashboard & Sync

- [x] Create FastAPI backend endpoints exposing threads, chat stream, and search.
- [x] Build minimalist web frontend connected to FastAPI via WebSockets.

### Sprint 5: Shared Memory Across All AI Tools

- [x] Store each conversation's source tool and project, with an in-place schema migration and idempotent imports.
- [x] Importers: Claude Code, Codex CLI, Gemini CLI, Cursor (local history, incremental).
- [x] Importers: ChatGPT, claude.ai (also covers Claude Desktop chats), Gemini (Google Takeout) data exports.
- [x] MCP server (`search_memory`, `recent_conversations`, `get_conversation`, `remember`).
- [x] `nexus connect` registers the MCP server with Claude Code, Claude Desktop, Cursor, Gemini CLI and Codex.
- [x] Background sync in the MCP server, web server and TUI, with a cross-process lock.
- [x] Memory browser: source badges and filters in the TUI and web UI, plus `nexus sync/import/search/status`.
- [ ] Browser extension for live capture from ChatGPT / claude.ai / Gemini web (exports cover this for now).
