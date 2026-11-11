# NexusAI

**One local memory shared by all your AI tools.** NexusAI collects your conversations from Claude Code, Codex, Gemini CLI, Cursor, ChatGPT, claude.ai and Gemini into one searchable store on your machine. Every MCP-capable agent can then recall them: ask Claude Code "what did we decide about auth last week?" and it can find the answer, even if that discussion happened in Cursor or ChatGPT.

- **Collects automatically:** local session logs from Claude Code, Codex, Gemini CLI and Cursor are imported in the background, incrementally.
- **Imports web chats:** ChatGPT, claude.ai (this also covers Claude Desktop chats, which are stored server-side) and Gemini (Google Takeout) data exports.
- **Gives it back to your agents** through an MCP server: `search_memory`, `recent_conversations`, `get_conversation`, `remember`.
- **Private by default:** SQLite plus a LanceDB vector index with a local embedding model. Nothing is uploaded, and no API key is needed for the memory itself.
- **Browse it:** a terminal UI and a web UI to search and read everything, filtered by tool. You can also chat with Claude or Gemini from them; those chats get the same memory as context.

## Quick start

```bash
uv venv -p 3.13 .venv && uv pip install -p .venv -e ".[dev]"

.venv/bin/nexus sync              # import your existing history (first run: model download + embedding)
.venv/bin/nexus connect           # see which AI tools are installed / connected
.venv/bin/nexus connect all       # give all of them access to the memory (MCP)
```

After `connect`, restart the tool (or open a new session). Agents will use `search_memory` when you refer to earlier work, and `remember` to save decisions. While any connected tool is running, new conversations keep syncing in the background.

### All your computers, one memory

```bash
nexus share                 # on your first computer: prints a pairing code
nexus join XXXX-XXXX-…      # on each other computer (after installing NexusAI there)
nexus connect all           # then give that computer's AI tools access
```

Memory syncs through a folder you already sync. The default is `iCloud Drive/NexusAI`; for Dropbox, Syncthing or anything else, pass a folder path to `share` and `join`.
- **Private:** everything in the folder is end-to-end encrypted (AES-256-GCM). The key lives only in the pairing code, so the cloud provider sees nothing readable.
- **No conflicts:** each computer writes only its own subfolder of small, immutable files, and reads everyone else's. Nothing can conflict or overwrite.
- **Merging:** the same conversation or note topic continued on two computers merges into one thread.
- **Always current:** a new computer gets the whole history on `join`. Running agents everywhere pick up other computers' memories within about a minute.
- **Any AI account:** memory belongs to you, not to an AI account, so it doesn't matter which one you're logged into.

`nexus devices` lists the computers; `nexus unshare` stops syncing this one.

### What gets imported

| Tool | How | Where it reads from |
|---|---|---|
| Claude Code | automatic | `~/.claude/projects/*/*.jsonl` |
| Codex CLI | automatic | `~/.codex/sessions/**/rollout-*.jsonl[.zst]` |
| Gemini CLI | automatic | `~/.gemini/tmp/*/chats/session-*` |
| Cursor | automatic | `Cursor/User/globalStorage/state.vscdb` (read-only) |
| ChatGPT | `nexus import <export.zip>` | Settings → Data controls → Export data |
| claude.ai / Claude Desktop | `nexus import <export.zip>` | Settings → Privacy → Export data |
| Gemini (web) | `nexus import <takeout.zip>` | Google Takeout → My Activity → *Gemini Apps*, format **JSON** |

Only what you and the assistant *said* is kept: tool calls, tool output, reasoning, injected system context and subagent chatter are skipped. Re-importing is safe, because only new messages are added.

### Commands

```
nexus                      terminal UI (memory browser + chat)
nexus sync [--watch]       import new local history now (or keep watching)
nexus import FILE...       import ChatGPT / claude.ai / Gemini Takeout exports
nexus search QUERY         search everything from the shell  [--source chatgpt]
nexus connect [TOOL|all]   claude-code, claude-desktop, cursor, gemini-cli, codex
nexus disconnect TOOL
nexus share [FOLDER]       sync memory across computers (prints a pairing code)
nexus join CODE [FOLDER]   add this computer;  nexus devices / nexus unshare
nexus status               what is stored per tool, sync state, connected tools
nexus mcp                  the MCP server (connected tools start it for you)
```

`connect` merges into each tool's existing config and writes a `.bak` copy first. For Claude Code it runs `claude mcp add --scope user nexus …`.

## Terminal UI

The sidebar shows every conversation with a source badge (`CC` Claude Code, `CX` Codex, `GC` Gemini CLI, `CU` Cursor, `GPT` ChatGPT, `CL` claude.ai, `GW` Gemini web, `NX` NexusAI, `NB` notes).

| Key / command | Action |
|---|---|
| `/search <query>`, `/open N` | Search all memory, open a result |
| `/history [source]` | Recent conversations, e.g. `/history chatgpt` |
| `/sync` | Import new history now |
| `Ctrl+N` / `/new`, `Ctrl+T` / `/model` | New NexusAI chat, switch Claude ↔ Gemini |
| `/rename`, `/delete` (twice), `/help`, `Ctrl+Q` | Manage, help, quit |

## Web UI & API

`.venv/bin/nexus-web`, then open http://127.0.0.1:8000. Use the search box and the "All tools" filter to browse memory.

| Endpoint | |
|---|---|
| `GET /api/sources` | Tools with conversation counts |
| `GET /api/threads?source=`, `GET /api/threads/{id}/messages` | Browse |
| `GET /api/search?q=&source=` | Semantic + keyword search |
| `POST /api/threads/{id}/chat` (SSE), `WS /ws` | Chat with Claude/Gemini (needs API keys) |

To reach it from other devices, set `NEXUS_HOST=0.0.0.0`, `NEXUS_ALLOWED_HOSTS` and `NEXUS_AUTH_TOKEN`, then open `http://<host>:8000/?token=<token>` once.

## Configuration

Everything comes from `.env` or the environment (see `.env.example`). API keys (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`) are only needed for chatting inside NexusAI. Data lives in `~/.nexus-ai` (`NEXUS_DATA_DIR`). Logs are in `nexus.log` and `mcp.log` there.

## Development

```bash
.venv/bin/pytest -q                  # everything offline: importers, MCP over stdio, TUI, web, browser e2e
.venv/bin/pytest -m integration      # live Claude/Gemini calls (needs keys + credit)
.venv/bin/ruff check . && .venv/bin/mypy src
```

Layout:
- `src/ingest`: one parser per tool, plus the incremental scanner
- `src/memory_mcp`: the MCP server
- `src/connect.py`: tool registration
- `src/core`: storage, search, chat service
- `src/providers`: LiteLLM router
- `src/cli`, `src/web`: the interfaces
