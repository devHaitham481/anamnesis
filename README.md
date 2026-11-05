# NexusAI

Local-first, multi-provider AI workspace. Chat with **Claude** and **Gemini** from a terminal UI or a web dashboard. Both interfaces share one local memory, so history is persisted, searchable, mirrored live between the terminal and the browser, and automatically recalled across threads and models.

- **One gateway:** every request goes through `ProviderRouter` (LiteLLM) and comes back in one shape (`CompletionResult` / `StreamChunk`).
- **Local memory:** SQLite (threads, messages, FTS5 keyword search) + LanceDB vectors with a local ONNX embedding model (`fastembed`, no API calls). Nothing leaves your machine except the prompt sent to the provider you pick.
- **RAG:** each prompt gets relevant snippets from *other* conversations (cosine ≥ 0.6), plus the recent thread history.
- **Two interfaces, one state:** Textual TUI and a FastAPI + WebSocket web UI. Messages written by either appear in the other within about a second.

## Quick start

```bash
uv venv -p 3.13 .venv            # Python ≥ 3.11
uv pip install -p .venv -e ".[dev]"
cp .env.example .env              # add ANTHROPIC_API_KEY and/or GEMINI_API_KEY

.venv/bin/python -m src.cli       # terminal UI        (or: .venv/bin/nexus)
.venv/bin/python -m src.web       # web UI on :8000     (or: .venv/bin/nexus-web)
```

On first start the embedding model (~130 MB) is downloaded once into `~/.nexus-ai/models`. If it can't be downloaded (e.g. offline), NexusAI still works with keyword-only memory and back-fills vectors on a later start.

## Terminal UI

| Key / command | Action |
|---|---|
| `Ctrl+N` / `/new [title]` | New thread |
| `Ctrl+T` / `/model [claude\|gemini]` | Switch model (per thread, mid-conversation is fine) |
| `Ctrl+L` | Focus the thread list (↑/↓, Enter to open) |
| `/history`, `/search <query>`, `/open N` | List threads / search all messages, then open item N |
| `/rename <title>`, `/delete` (twice to confirm) | Manage the current thread |
| `/help`, `Ctrl+Q` | Help, quit |

Logs go to `~/.nexus-ai/nexus.log`.

## Web UI & API

Open http://127.0.0.1:8000. Double-click the title to rename a thread. Use the search box to search all chats.

| Endpoint | |
|---|---|
| `GET /api/health`, `GET /api/models` | Status, model availability (reports missing keys) |
| `GET/POST /api/threads`, `GET/PATCH/DELETE /api/threads/{id}` | Threads |
| `GET /api/threads/{id}/messages` | Messages |
| `GET /api/search?q=` | Semantic + keyword search |
| `POST /api/threads/{id}/chat` | One chat turn as **Server-Sent Events** |
| `WS /ws` | `{"type":"send","thread_id","content","model"}` → broadcast `stream_start` / `delta` / `message` / `stream_end` / `threads_changed` |

```bash
T=$(curl -s -XPOST localhost:8000/api/threads -H 'content-type: application/json' -d '{}' | jq -r .id)
curl -N -XPOST localhost:8000/api/threads/$T/chat -H 'content-type: application/json' -d '{"content":"Hello"}'
```

**Using it from other devices:** the server binds to `127.0.0.1` by default. To reach it from your phone or another laptop, set `NEXUS_HOST=0.0.0.0`, list the hostnames/IPs you'll use in `NEXUS_ALLOWED_HOSTS`, and set `NEXUS_AUTH_TOKEN`. Then open `http://<host>:8000/?token=<token>` once on each device (the token is remembered). Cross-origin WebSocket connections are always rejected.

## Configuration

All settings come from `.env` or the environment (see `.env.example`). Keys are never hard-coded or logged.

## Development

```bash
.venv/bin/pytest -q                       # unit, TUI (pilot), web and e2e browser tests
.venv/bin/pytest -m integration           # live Claude/Gemini calls (needs keys)
.venv/bin/ruff check --fix . && .venv/bin/mypy src
uv pip install -p .venv -e ".[e2e]" && .venv/bin/playwright install chromium   # enables the browser test
```

Layout: `src/providers` (router), `src/core` (db, vector, context, chat service), `src/cli` (TUI), `src/web` (API + static UI). The TUI and web layers talk only to `ChatService`, never to the database directly.
