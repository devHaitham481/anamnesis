"""Unified provider gateway. Every model request in NexusAI goes through `ProviderRouter`."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from typing import Any

import litellm

from src.config import Settings, load_settings
from src.providers.models import ChatMessage, CompletionResult, StreamChunk, Usage

CompletionFn = Callable[..., Awaitable[Any]]

# LiteLLM model prefix -> provider name used for key lookup and tagging.
_PREFIX_TO_PROVIDER = {"anthropic": "anthropic", "gemini": "gemini"}


class UnknownModelError(ValueError):
    """Raised when a model alias / string cannot be mapped to a supported provider."""


class ProviderRouter:
    """Routes chat completions to Claude or Gemini via LiteLLM and normalizes the output."""

    def __init__(
        self,
        settings: Settings | None = None,
        completion_fn: CompletionFn | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self._completion = completion_fn or litellm.acompletion
        self.aliases: dict[str, str] = {
            "claude": self.settings.claude_model,
            "gemini": self.settings.gemini_model,
        }

    def resolve(self, model: str) -> tuple[str, str]:
        """Return (provider, litellm_model) for an alias like 'claude' or a full model string."""
        full = self.aliases.get(model, model)
        prefix, sep, _ = full.partition("/")
        provider = _PREFIX_TO_PROVIDER.get(prefix) if sep else None
        if provider is None:
            raise UnknownModelError(
                f"Unsupported model {model!r}; use an alias {sorted(self.aliases)} "
                "or a string prefixed with 'anthropic/' or 'gemini/'."
            )
        return provider, full

    def _request_kwargs(
        self, model: str, messages: Sequence[ChatMessage], **params: Any
    ) -> tuple[str, str, dict[str, Any]]:
        provider, full = self.resolve(model)
        kwargs: dict[str, Any] = {
            "model": full,
            "messages": [m.model_dump() for m in messages],
            "api_key": self.settings.require_key(provider),
            **params,
        }
        return provider, full, kwargs

    async def complete(
        self, messages: Sequence[ChatMessage], model: str = "claude", **params: Any
    ) -> CompletionResult:
        provider, full, kwargs = self._request_kwargs(model, messages, **params)
        response = await self._completion(**kwargs)
        choice = response.choices[0]
        usage = getattr(response, "usage", None)
        return CompletionResult(
            provider=provider,
            model=full,
            content=choice.message.content or "",
            finish_reason=choice.finish_reason,
            usage=Usage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage, "total_tokens", 0) or 0,
            ),
        )

    async def stream(
        self, messages: Sequence[ChatMessage], model: str = "claude", **params: Any
    ) -> AsyncGenerator[StreamChunk, None]:
        provider, full, kwargs = self._request_kwargs(model, messages, stream=True, **params)
        response = await self._completion(**kwargs)
        try:
            async for chunk in response:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = getattr(choice.delta, "content", None) or ""
                if not delta and not choice.finish_reason:
                    continue
                yield StreamChunk(
                    provider=provider, model=full, delta=delta, finish_reason=choice.finish_reason
                )
        finally:
            aclose = getattr(response, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()


# PRD naming.
ProviderManager = ProviderRouter
