# Anamnesis

**One local memory shared by all your AI tools.** Anamnesis collects your conversations from Claude Code, Codex, Gemini CLI, Cursor, ChatGPT, claude.ai and Gemini into one searchable store on your machine. Every MCP-capable agent can then recall them: ask Claude Code "what did we decide about auth last week?" and it can find the answer, even if that discussion happened in Cursor or ChatGPT.

- **Collects automatically:** local session logs from Claude Code, Codex, Gemini CLI and Cursor are imported in the background, incrementally.
- **Imports web chats:** ChatGPT, claude.ai (this also covers Claude Desktop chats, which are stored server-side) and Gemini (Google Takeout) data exports.
- **Gives it back to your agents** through an MCP server: `search_memory`, `recent_conversations`, `get_conversation`, `remember`.
- **Private by default:** SQLite plus a LanceDB vector index with a local embedding model. Nothing is uploaded, and no API keys are needed.
- **Browse it:** a terminal UI and a web UI to search and read everything, filtered by tool.

Anamnesis does one thing: it is the memory. It is not a chat client and never calls a model; you keep talking to the tools you already use.

## Quick start

```bash
uv venv -p 3.13 .venv && uv pip install -p .venv -e ".[dev]"

.venv/bin/anam sync              # import your existing history (first run: model download + embedding)
.venv/bin/anam connect           # see which AI tools are installed / connected
.venv/bin/anam connect all       # give all of them access to the memory (MCP)
```

After `connect`, restart the tool (or open a new session). Agents will use `search_memory` when you refer to earlier work, and `remember` to save decisions. While any connected tool is running, new conversations keep syncing in the background.

### All your computers, one memory

```bash
anam share                 # on your first computer: prints a pairing code
anam join XXXX-XXXX-…      # on each other computer (after installing Anamnesis there)
anam connect all           # then give that computer's AI tools access
```

Memory syncs through a folder you already sync. The default is `iCloud Drive/Anamnesis`; for Dropbox, Syncthing or anything else, pass a folder path to `share` and `join`.
- **Private:** everything in the folder is end-to-end encrypted (AES-256-GCM). The key lives only in the pairing code, so the cloud provider sees nothing readable.
- **No conflicts:** each computer writes only its own subfolder of small, immutable files, and reads everyone else's. Nothing can conflict or overwrite.
- **Merging:** the same conversation or note topic continued on two computers merges into one thread.
- **Always current:** a new computer gets the whole history on `join`. Running agents everywhere pick up other computers' memories within about a minute.
- **Any AI account:** memory belongs to you, not to an AI account, so it doesn't matter which one you're logged into.

`anam devices` lists the computers; `anam unshare` stops syncing this one.

### What gets imported

| Tool | How | Where it reads from |
|---|---|---|
| Claude Code | automatic | `~/.claude/projects/*/*.jsonl` |
| Codex CLI | automatic | `~/.codex/sessions/**/rollout-*.jsonl[.zst]` |
| Gemini CLI | automatic | `~/.gemini/tmp/*/chats/session-*` |
| Cursor | automatic | `Cursor/User/globalStorage/state.vscdb` (read-only) |
| ChatGPT | `anam import <export.zip>` | Settings → Data controls → Export data |
| claude.ai / Claude Desktop | `anam import <export.zip>` | Settings → Privacy → Export data |
| Gemini (web) | `anam import <takeout.zip>` | Google Takeout → My Activity → *Gemini Apps*, format **JSON** |

Only what you and the assistant *said* is kept: tool calls, tool output, reasoning, injected system context and subagent chatter are skipped. Re-importing is safe, because only new messages are added.

### Commands

```
anam                      terminal memory browser
anam sync [--watch]       import new local history now (or keep watching)
anam import FILE...       import ChatGPT / claude.ai / Gemini Takeout exports
anam search QUERY         search everything from the shell  [--source chatgpt]
anam connect [TOOL|all]   claude-code, claude-desktop, cursor, gemini-cli, codex
anam disconnect TOOL
anam share [FOLDER]       sync memory across computers (prints a pairing code)
anam join CODE [FOLDER]   add this computer;  anam devices / anam unshare
anam status               what is stored per tool, sync state, connected tools
anam mcp                  the MCP server (connected tools start it for you)
```

`connect` merges into each tool's existing config and writes a `.bak` copy first. For Claude Code it runs `claude mcp add --scope user anamnesis …`.

## Terminal UI

The sidebar shows every conversation with a source badge (`CC` Claude Code, `CX` Codex, `GC` Gemini CLI, `CU` Cursor, `GPT` ChatGPT, `CL` claude.ai, `GW` Gemini web, `NB` notes saved with `remember`, `AN` chats from the old built-in chat). Typing in the bottom box searches all memory.

| Key / command | Action |
|---|---|
| *text* or `/search <query>`, `/open N` | Search all memory, open a result |
| `/history [source]` | Recent conversations, e.g. `/history chatgpt` |
| `/sync` | Import new history now |
| `Ctrl+L` / `Ctrl+F` | Focus the conversation list / the search box |
| `/rename`, `/delete` (twice), `/help`, `Ctrl+Q` | Manage, help, quit |

## Web UI & API

`.venv/bin/anam-web`, then open http://127.0.0.1:8000. Use the search box and the "All tools" filter to browse memory.

| Endpoint | |
|---|---|
| `GET /api/sources` | Tools with conversation counts |
| `GET /api/threads?source=`, `GET /api/threads/{id}/messages` | Browse |
| `GET /api/search?q=&source=` | Semantic + keyword search |
| `PATCH`/`DELETE /api/threads/{id}` | Rename / delete a conversation |
| `WS /ws` | Live updates as new history is imported |

To reach it from other devices, set `ANAMNESIS_HOST=0.0.0.0`, `ANAMNESIS_ALLOWED_HOSTS` and `ANAMNESIS_AUTH_TOKEN`, then open `http://<host>:8000/?token=<token>` once.

## Configuration

Everything comes from `.env` or the environment (see `.env.example`); nothing is required. Data lives in `~/.anamnesis` (`ANAMNESIS_DATA_DIR`). Logs are in `anamnesis.log` and `mcp.log` there.

## Development

```bash
.venv/bin/pytest -q                  # everything offline: importers, MCP over stdio, TUI, web, browser e2e
.venv/bin/ruff check . && .venv/bin/mypy src
```

Layout:
- `src/ingest`: one parser per tool, plus the incremental scanner
- `src/memory_mcp`: the MCP server
- `src/connect.py`: tool registration
- `src/core`: storage, search, the memory service, cross-device sync
- `src/cli`, `src/web`: the interfaces
