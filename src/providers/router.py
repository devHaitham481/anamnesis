"""Unified provider gateway. Every model request in Anamnesis goes through `ProviderRouter`."""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from typing import Any

import litellm
from litellm.exceptions import (
    APIConnectionError,
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

from src.config import Settings, load_settings
from src.providers.models import ChatMessage, CompletionResult, StreamChunk, Usage

CompletionFn = Callable[..., Awaitable[Any]]

# LiteLLM model prefix -> provider name used for key lookup and tagging.
_PREFIX_TO_PROVIDER = {"anthropic": "anthropic", "gemini": "gemini"}


_PROVIDER_LABEL = {"anthropic": "Claude", "gemini": "Gemini"}


class UnknownModelError(ValueError):
    """Raised when a model alias / string cannot be mapped to a supported provider."""


class ProviderError(RuntimeError):
    """A provider request failed. `str()` is a short, human-readable explanation."""

    def __init__(self, message: str, *, provider: str, status: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status


def _provider_detail(exc: BaseException) -> str:
    """Pull the provider's own error message out of LiteLLM's verbose exception text."""
    text = str(getattr(exc, "message", None) or exc)
    for match in re.finditer(r"\{.*\}", text, re.DOTALL):
        try:
            body = json.loads(match.group(0))
        except ValueError:
            continue
        err = body.get("error", body) if isinstance(body, dict) else None
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return " ".join(err["message"].split())
    m = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        return " ".join(m.group(1).encode().decode("unicode_escape", "ignore").split())
    text = re.sub(r"^(litellm\.)?\w+(Error|Exception)?:\s*", "", text.strip())
    return " ".join(text.split())[:300]


def to_provider_error(exc: Exception, provider: str, model: str) -> ProviderError:
    """Translate a LiteLLM/transport exception into a concise `ProviderError`."""
    label = _PROVIDER_LABEL.get(provider, provider)
    status = getattr(exc, "status_code", None)
    status = status if isinstance(status, int) else None
    detail = _provider_detail(exc)
    if isinstance(exc, Timeout):
        msg = f"{label} did not respond in time. Try again, or switch model."
    elif isinstance(exc, AuthenticationError) or status in (401, 403):
        msg = f"{label} rejected the API key: {detail}"
    elif isinstance(exc, RateLimitError) or status == 429:
        msg = f"{label} rate limit or quota reached: {detail}"
    elif isinstance(exc, ServiceUnavailableError) or status in (500, 502, 503, 529):
        msg = f"{label} is temporarily unavailable: {detail}"
    elif isinstance(exc, NotFoundError) or status == 404:
        msg = (
            f"{label} model {model!r} is not available: {detail} "
            "(set a different model in .env, e.g. ANAMNESIS_GEMINI_MODEL / ANAMNESIS_CLAUDE_MODEL)"
        )
    elif isinstance(exc, APIConnectionError):
        msg = f"Could not reach {label}: {detail}"
    else:
        msg = f"{label} request failed: {detail}"
    return ProviderError(msg, provider=provider, status=status)


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
            "timeout": self.settings.request_timeout,
            "num_retries": self.settings.max_retries,
            **params,
        }
        return provider, full, kwargs

    async def complete(
        self, messages: Sequence[ChatMessage], model: str = "claude", **params: Any
    ) -> CompletionResult:
        provider, full, kwargs = self._request_kwargs(model, messages, **params)
        try:
            response = await self._completion(**kwargs)
        except Exception as e:
            raise to_provider_error(e, provider, full) from e
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
        try:
            response = await self._completion(**kwargs)
        except Exception as e:
            raise to_provider_error(e, provider, full) from e
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
        except ProviderError:
            raise
        except Exception as e:  # mid-stream failure (overload, connection drop, ...)
            raise to_provider_error(e, provider, full) from e
        finally:
            aclose = getattr(response, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()


# PRD naming.
ProviderManager = ProviderRouter
