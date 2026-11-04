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

- [ ] Create SQLite database manager (`/src/core/db.py`) for thread logging.
- [ ] Set up LanceDB embedding indexer (`/src/core/vector.py`).
- [ ] Build RAG context-assembly function to inject history into new prompts.

### Sprint 3: Terminal UI (TUI)

- [ ] Create Textual TUI app shell in `/src/cli/app.py`.
- [ ] Implement active thread list sidebar and real-time streaming chat canvas.
- [ ] Add model switching dropdown/hotkey (Claude ↔ Gemini).

### Sprint 4: Web Dashboard & Sync

- [ ] Create FastAPI backend endpoints exposing threads, chat stream, and search.
- [ ] Build minimalist web frontend connected to FastAPI via WebSockets.
