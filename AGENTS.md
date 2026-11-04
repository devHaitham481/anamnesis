# AGENTS.md - Agent Roles & Delegation Rules

## 1. System Roles

### Lead Orchestrator

- **Responsibility:** Reads `PRD.md`, breaks down requirements, creates tasks in `TASKS.md`, and delegates implementation steps.
- **Constraint:** Never writes massive monolithic files. Keeps code strictly modular.

### Backend & Gateway Specialist

- **Focus Directory:** `/src/core`, `/src/providers`
- **Responsibility:** Implements SQLite schemas, LanceDB vector retrieval, and LiteLLM wrappers. Ensure stream compatibility for both Gemini and Claude.

### Interface Developer (TUI & Web)

- **Focus Directory:** `/src/cli`, `/src/web`
- **Responsibility:** Builds the Textual TUI interface and FastAPI WebSocket endpoints. Ensures instant state synchronization between terminal and web client.

## 2. Verification Protocol

Before marking any task as finished:

1. Verify that the code runs without runtime errors.
2. Ensure new functions have test coverage.
3. Verify that changes do not break existing multi-provider compatibility.
