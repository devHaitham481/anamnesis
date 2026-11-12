# Product Requirements Document (PRD)

## Project Name: Anamnesis (formerly NexusAI) — one local memory shared by all your AI tools

---

## 1. Executive Summary

Anamnesis is a local-first, single-purpose memory layer. It collects the conversations you have in the AI tools you already use (Claude Code, Codex, Gemini CLI, Cursor, ChatGPT, claude.ai, Gemini) into one searchable store on your machine, and gives every MCP-capable agent read and write access to it. Context from any conversation is available in every other tool, without re-explaining.

**Non-goal:** Anamnesis is not a chat client or model gateway. It never calls an LLM and needs no API keys. (An earlier version had a built-in Claude/Gemini chat; it was removed on 2026-11-12. Chats stored by it remain in existing databases as ordinary memory.)

---

## 2. Target Users & Core Value Proposition

- **Developers & power users** who switch between several AI tools and lose context at every switch.
- **Privacy-conscious users:** history, search index and embedding model stay on disk; cross-device sync is end-to-end encrypted.

---

## 3. Architecture

### Module 1: Memory core (`/src/core`)

- **Storage:** SQLite (`aiosqlite`) holding threads (with `source`, `project`, `external_id`) and messages, with FTS5 keyword search.
- **Semantic search:** LanceDB with a local embedding model (`fastembed`), merged with keyword hits.
- **`MemoryService`:** the single entry point for every interface (import, remember, search, browse).
- **Cross-device sync:** encrypted per-device logs in a synced folder.

### Module 2: Ingestion (`/src/ingest`)

- Incremental, idempotent importers for local tool history (Claude Code, Codex, Gemini CLI, Cursor) and data exports (ChatGPT, claude.ai, Gemini Takeout).
- Background sync with a cross-process lock.

### Module 3: Access (`/src/memory_mcp`, `/src/connect.py`)

- **MCP server:** `search_memory`, `recent_conversations`, `get_conversation`, `remember`.
- **`anam connect`:** registers the MCP server with each installed tool.

### Module 4: Browsing (`/src/cli`, `/src/web`)

- **CLI:** `anam sync / import / search / status / connect / share / join`.
- **TUI (`Textual`)** and **web UI (`FastAPI` + WebSocket)**: read-only memory browsers with search, source filters, rename/delete and live updates.

---

## 4. Requirements & Milestones

### Done

- [x] Storage, keyword + semantic search, idempotent imports with in-place schema migration.
- [x] Importers for Claude Code, Codex, Gemini CLI, Cursor, ChatGPT, claude.ai, Gemini Takeout.
- [x] MCP server and `anam connect` for Claude Code, Claude Desktop, Cursor, Gemini CLI, Codex.
- [x] Background sync; TUI and web memory browsers.
- [x] Encrypted cross-device memory (`anam share` / `join` / `devices`).
- [x] Single purpose: chat gateway removed.

### Next

- [ ] Browser extension for live capture from ChatGPT / claude.ai / Gemini web.
- [ ] Propagate deletions across devices.
- [ ] Optional hosted relay for cross-device sync.

---

## 5. Non-Functional Requirements

- **Performance:** DB lookups and searches complete in under 50 ms; imports never block searching (text first, embeddings in the background).
- **Privacy:** all data and indexes stay local by default; nothing is sent to any model provider.
- **Quality:** modular, strictly typed (Pydantic, mypy strict), tested offline.
