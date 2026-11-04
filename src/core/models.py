"""Persistent domain models shared by the core, TUI and web layers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Role = Literal["system", "user", "assistant"]


class Thread(BaseModel):
    id: str
    title: str
    model: str
    system_prompt: str | None = None
    created_at: float
    updated_at: float


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
