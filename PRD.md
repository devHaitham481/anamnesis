# Product Requirements Document (PRD)

## Project Name: NexusAI (Unified Multi-Provider AI Platform & CLI)

---

## 1. Executive Summary

NexusAI is an open-source, local-first platform designed to unify multiple AI model providers (Claude, Gemini, OpenAI, Ollama) into a single workspace. It provides both a Command Line/TUI interface and a Web Dashboard backed by a persistent local memory layer. This ensures that chat context, session history, and prompt interactions are preserved and searchable across all providers and user interfaces.

---

## 2. Target Users & Core Value Proposition

- **Developers & Power Users:** Work across multiple CLI tools and LLMs without losing history or re-explaining context.
- **Privacy-Conscious Builders:** Prefer keeping session databases and embeddings locally on disk.
- **Multi-Model Testers:** Need to compare answers from Claude 3.5 Sonnet and Gemini 1.5/2.5 Pro using identical chat history.

---

## 3. Core Technical Architecture & Specs

### Module 1: The Context & Memory Core (`/src/core`)

- **Database:** SQLite (using `aiosqlite` or `better-sqlite3`) storing raw chat threads, agent messages, provider tags, and system prompts.
- **Vector Search Engine:** Integrated local vector engine (`LanceDB` or `ChromaDB`) to index session history automatically for hybrid/semantic RAG retrieval.
- **Model Context Protocol (MCP) Server:** Implement an internal MCP server so terminal instances and sub-agents can automatically query and write to the central memory store.

### Module 2: Unified Provider Gateway (`/src/providers`)

- **API Normalizer:** Utilize `LiteLLM` (or a standardized gateway) to route requests uniformly to Anthropic, Google Gemini, OpenAI, and Ollama.
- **Streaming Handlers:** Standardize Server-Sent Events (SSE) and terminal stdout chunking.

### Module 3: Interfaces (`/src/cli` & `/src/web`)

- **Terminal UI (TUI):** Interactive terminal layout built with Python `Textual` (or Node `Ink`) featuring multi-model switching, Markdown formatting, and context search.
- **Web Interface:** Lightweight web frontend (FastAPI + Next.js or React) displaying chat sessions live, synchronized with the SQLite database.

---

## 4. Functional Requirements & Milestones

### Milestone 1: Core Database & LiteLLM Gateway

- [ ] Initialize SQLite schema for `threads`, `messages`, and `providers`.
- [ ] Implement `ProviderManager` wrapping LiteLLM for streaming completions.
- [ ] Write integration test verifying Claude and Gemini queries return unified output structures.

### Milestone 2: Context Memory & RAG Layer

- [ ] Set up vector database (`LanceDB`) to auto-embed user and assistant exchanges.
- [ ] Create context retrieval pipeline that injects relevant past chat snippets when prompting.

### Milestone 3: Terminal UI (TUI)

- [ ] Build terminal interface with model selection, active thread list, and live streaming response box.
- [ ] Add slash-commands: `/model <name>`, `/history`, `/search <query>`.

### Milestone 4: Web UI & Multi-Device Sync

- [ ] Fast API / Node REST + WebSocket server exposing core database and streaming endpoints.
- [ ] Web chat UI that mirrors terminal chats in real-time.

---

## 5. Non-Functional Requirements

- **Performance:** DB lookups and vector checks must complete in under 50ms.
- **Data Privacy:** All database and vector indexes stay local by default.
- **Self-Documenting:** Clean modular architecture with strict typing (`TypeScript` or Python `Pydantic`).
