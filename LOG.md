# LOG.md - Implementation Log

## 2026-11-05 — Sprint 1: Setup & Core Gateway

- **Project setup:** `pyproject.toml` (Python ≥3.11, litellm, pydantic, python-dotenv; dev: pytest, pytest-asyncio, ruff, mypy). Package skeleton `src/{core,providers,cli,web}`, `tests/`, `.gitignore`, `.env.example`. Dev env: `uv venv -p 3.13 .venv && uv pip install -e ".[dev]"` (system Python is 3.9).
- **Config (`src/config.py`):** `load_settings()` merges `.env` with the process environment (env wins); keys stored as `SecretStr`; `Settings.require_key()` raises `MissingAPIKeyError`. Model defaults overridable via `NEXUS_CLAUDE_MODEL` / `NEXUS_GEMINI_MODEL`.
- **Router (`src/providers/router.py`):** `ProviderRouter` (alias `ProviderManager`) resolves `claude`/`gemini` aliases or `anthropic/…`/`gemini/…` strings, injects the key, and normalizes LiteLLM output into `CompletionResult` / `StreamChunk` (`src/providers/models.py`). Completion function is injectable for testing.
- **Tests:** 16 unit tests (config + router via LiteLLM `mock_response`, no network). 4 live integration tests for Claude & Gemini (complete + stream) are **skipped** — no `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` in the environment. Add them to `.env` and run `pytest -m integration`.
- **Open item:** default model IDs (`anthropic/claude-sonnet-5-5`, `gemini/gemini-2.5-flash`) are unverified against live APIs until keys are provided.
