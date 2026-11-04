"""Provider-agnostic data structures returned by the router."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Role = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    role: Role
    content: str


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class CompletionResult(BaseModel):
    provider: str
    model: str
    content: str
    finish_reason: str | None = None
    usage: Usage = Usage()


class StreamChunk(BaseModel):
    provider: str
    model: str
    delta: str
    finish_reason: str | None = None
