# CLAUDE.md - Workspace Instructions for Claude Code

## 1. Project Stack & Principles

- **Language:** Python 3.11+ (or Node.js 20+ TypeScript — check root setup)
- **Purpose:** a single-purpose local memory shared by all AI tools (MCP server + importers). It is not a chat client and never calls a model.
- **Database:** SQLite + LanceDB (Vector)
- **TUI Framework:** `Textual`
- **Backend API:** `FastAPI` + WebSockets (memory browser)
- **Execution Style:** Local-first, modular, fully typed, heavily tested.

## 2. Autonomous Execution Workflow

When assigned a task or milestone:

1. Read `PRD.md` and check `TASKS.md` for current progress.
2. Select the next uncompleted task.
3. Write modular, typed code in small, reviewable commits.
4. Run unit tests and syntax checks after every edit.
5. Update `TASKS.md` with status (`[x]`) and write a brief entry in `LOG.md`.
6. DO NOT pause for human approval on minor implementation details — test and proceed continuously unless blocked by missing environment keys.

## 3. Essential Commands

- **Install Dependencies:** `pip install -e .` or `pnpm install`
- **Run Tests:** `pytest` or `pnpm test`
- **Lint & Format:** `ruff check --fix .` or `pnpm lint`
- **Run CLI:** `python -m src.cli`
- **Run Web Server:** `uvicorn src.web.main:app --reload`

## 4. Quality Rules

- No hardcoded secrets; configuration comes from `.env` / the environment.
- Every interface (MCP, CLI, TUI, web) goes through `MemoryService`; don't add chat or model-calling features.
- Keep terminal UI logic isolated from database operations.
