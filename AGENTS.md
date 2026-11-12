# AGENTS.md - Agent Roles & Delegation Rules

## 1. System Roles

### Lead Orchestrator

- **Responsibility:** Reads `PRD.md`, breaks down requirements, creates tasks in `TASKS.md`, and delegates implementation steps.
- **Constraint:** Never writes massive monolithic files. Keeps code strictly modular.

### Memory & Ingestion Specialist

- **Focus Directory:** `/src/core`, `/src/ingest`, `/src/memory_mcp`
- **Responsibility:** Implements SQLite schemas, LanceDB retrieval, importers for each AI tool, cross-device sync and the MCP server.

### Interface Developer (TUI & Web)

- **Focus Directory:** `/src/cli`, `/src/web`
- **Responsibility:** Builds the Textual TUI and FastAPI web memory browsers (read, search, manage). Ensures new memory shows up live in both.

## 2. Verification Protocol

Before marking any task as finished:

1. Verify that the code runs without runtime errors.
2. Ensure new functions have test coverage.
3. Verify that changes keep Anamnesis single-purpose (memory only, no model calls) and that every importer still works.
