"""Persistent domain models shared by the core, TUI and web layers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Role = Literal["system", "user", "assistant"]


# Where a conversation came from. "anamnesis" = chatted inside Anamnesis; the rest are imported.
SOURCE_LABELS: dict[str, str] = {
    "anamnesis": "Anamnesis chat",
    "note": "Memory note",
    "claude-code": "Claude Code",
    "codex": "Codex CLI",
    "gemini-cli": "Gemini CLI",
    "cursor": "Cursor",
    "chatgpt": "ChatGPT",
    "claude-web": "Claude (web/desktop)",
    "gemini-web": "Gemini (web)",
}


class Thread(BaseModel):
    id: str
    title: str
    model: str
    system_prompt: str | None = None
    created_at: float
    updated_at: float
    source: str = "anamnesis"
    project: str | None = None
    external_id: str | None = None


class Message(BaseModel):
    id: int
    thread_id: str
    role: Role
    content: str
    provider: str | None = None
    model: str | None = None
    created_at: float


class SearchHit(BaseModel):
    message_id: int
    thread_id: str
    role: Role
    content: str
    score: float
    thread_title: str | None = None
    source: str | None = None
    project: str | None = None
    created_at: float | None = None


class ImportedMessage(BaseModel):
    """One message parsed from another tool's history. `external_id` must be stable."""

    external_id: str
    role: Role
    content: str
    created_at: float
    model: str | None = None


class ImportedConversation(BaseModel):
    """One conversation/session parsed from another tool (Claude Code, ChatGPT export, ...)."""

    source: str
    external_id: str
    title: str | None = None
    project: str | None = None
    messages: list[ImportedMessage]
