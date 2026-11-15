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

### Sprint 8: Launch: Install & Setup

Goal: someone who has never heard of Anamnesis goes from the website to a working memory with one command.

- [x] Rename the package from `src` to `anamnesis` (src layout), so installing it can't clash with other tools.
- [x] `install.sh` (served by GitHub Pages): installs uv if needed, Anamnesis with its own Python, then runs setup.
- [x] `anam setup`: detect tools, import history, ask before connecting, say what's next. Safe to re-run.
- [x] `anam connect` repairs entries that point at an old install; `anam status` shows them as outdated.
- [x] `anam disconnect all`, `anam --version`.
- [x] Prebuilt node image on GHCR (amd64 + arm64); docker-compose and `anam node deploy fly` use it, no clone needed.
- [x] CI (lint, types, tests on 3.11 and 3.13); README and website install guides.
- [x] PyPI name `anamnesis-cli`, package metadata, release workflow (trusted publishing on `v*` tags).
- [ ] First PyPI release: add the pending publisher on pypi.org, then tag `v0.1.0`; then point `install.sh` at PyPI.

### Sprint 9: The Archive

Goal: lead with a concrete promise, "never lose an AI conversation again", and show that it holds.

- [x] `anam status` leads with the archive: conversations and messages per tool, activity this week, sessions kept after the tool deleted them, and which web-chat exports are missing (with where to get them).
- [x] Claude Code's 30-day deletion: detected; `anam setup` asks to turn it off, `anam keep` does it later (merged into `~/.claude/settings.json`, `.bak` kept).
- [x] Agent lookups over local MCP are counted (`lookups` table) and shown in `anam status`, to see whether agents actually use the memory.
- [x] Sharper MCP instructions: search before answering when a project, ticket, system or earlier decision comes up.
- [x] Setup ends with the archive and export guidance; `anam import -h` lists where each export lives.
- [x] README and website lead with the archive.
- [ ] Measure for a week: agent lookups per week before and after the sharper instructions.
- [ ] Claude Code plugin (instructions + SessionStart/SessionEnd hooks), then the browser extension for live web chats.
