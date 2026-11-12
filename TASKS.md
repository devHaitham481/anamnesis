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
- [x] `anam connect` registers the MCP server with Claude Code, Claude Desktop, Cursor, Gemini CLI and Codex.
- [x] Background sync in the MCP server, web server and TUI, with a cross-process lock.
- [x] Memory browser: source badges and filters in the TUI and web UI, plus `anam sync/import/search/status`.
- [x] Cross-device memory: encrypted per-device logs in a synced folder (`anam share` / `join` / `devices`).
- [ ] Browser extension for live capture from ChatGPT / claude.ai / Gemini web (exports cover this for now).

### Sprint 6: Single Purpose

- [x] Remove the chat gateway (LiteLLM router, chat service, model switching, chat UI in TUI and web, API keys). The TUI and web UI are now memory browsers only.

### Sprint 7: Always-on Node (self-hosted) + Web Chat Connectors

Goal: claude.ai / ChatGPT (web + mobile) can search memory and save notes even when every computer is asleep. Each user runs their own node; there is no hosted Anamnesis service.

- [x] Sync storage abstraction: folder (iCloud, Dropbox, …) or node, same encrypted segment format.
- [x] `anam node serve`: sync hub for all computers (HTTPS API), keeps its own searchable copy.
- [x] `anam node link URL`: link a computer with the node password; choose what to upload (all / exclude projects / notes only).
- [x] Remote MCP at `/mcp` with built-in OAuth (password sign-in page; read-only or read + save notes).
- [x] `anam connect claude-web` / `chatgpt-web`: print URL + setup steps.
- [x] `anam node activity` / `revoke` / `wipe`.
- [x] Dockerfile + docker-compose; `fly.toml` + `anam node deploy fly`.
- [x] Docs: self-hosting guide.
