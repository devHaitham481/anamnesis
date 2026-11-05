"""Router tests.

Unit tests run LiteLLM's real code path with `mock_response` (no network).
Integration tests hit Claude and Gemini and are skipped when the API keys are not set.
"""

from functools import partial

import litellm
import pytest

from src.config import MissingAPIKeyError, Settings, load_settings
from src.providers import ChatMessage, CompletionResult, ProviderRouter, StreamChunk
from src.providers.router import ProviderError, UnknownModelError

MESSAGES = [ChatMessage(role="user", content="Reply with exactly the word: pong")]
FAKE = Settings(anthropic_api_key="fake-ant", gemini_api_key="fake-gem")  # type: ignore[arg-type]


def mock_router(reply: str = "pong") -> ProviderRouter:
    return ProviderRouter(FAKE, completion_fn=partial(litellm.acompletion, mock_response=reply))


# ---------- unit ----------


@pytest.mark.parametrize(
    ("alias", "provider", "model"),
    [
        ("claude", "anthropic", FAKE.claude_model),
        ("gemini", "gemini", FAKE.gemini_model),
        ("gemini/gemini-2.5-pro", "gemini", "gemini/gemini-2.5-pro"),
    ],
)
def test_resolve(alias: str, provider: str, model: str) -> None:
    assert ProviderRouter(FAKE).resolve(alias) == (provider, model)


@pytest.mark.parametrize("bad", ["gpt-4o", "openai/gpt-4o", "nonsense"])
def test_resolve_rejects_unsupported(bad: str) -> None:
    with pytest.raises(UnknownModelError):
        ProviderRouter(FAKE).resolve(bad)


async def test_missing_key_raises_before_request() -> None:
    called = False

    async def fn(**_: object) -> None:
        nonlocal called
        called = True

    router = ProviderRouter(Settings(), completion_fn=fn)
    with pytest.raises(MissingAPIKeyError):
        await router.complete(MESSAGES, model="claude")
    assert not called


async def test_passes_key_and_params() -> None:
    captured: dict[str, object] = {}

    async def fn(**kwargs: object) -> object:
        captured.update(kwargs)
        return await litellm.acompletion(**kwargs, mock_response="ok")

    await ProviderRouter(FAKE, completion_fn=fn).complete(MESSAGES, model="gemini", temperature=0)
    assert captured["api_key"] == "fake-gem"
    assert captured["temperature"] == 0
    assert captured["messages"] == [{"role": "user", "content": MESSAGES[0].content}]


@pytest.mark.parametrize("alias", ["claude", "gemini"])
async def test_complete_unified_shape(alias: str) -> None:
    result = await mock_router().complete(MESSAGES, model=alias)
    assert isinstance(result, CompletionResult)
    assert result.content == "pong"
    assert result.provider == ProviderRouter(FAKE).resolve(alias)[0]


@pytest.mark.parametrize("alias", ["claude", "gemini"])
async def test_stream_unified_shape(alias: str) -> None:
    chunks = [c async for c in mock_router("hello streaming world").stream(MESSAGES, model=alias)]
    assert chunks and all(isinstance(c, StreamChunk) for c in chunks)
    assert "".join(c.delta for c in chunks) == "hello streaming world"


async def test_timeout_and_retries_default_from_settings() -> None:
    seen: dict[str, object] = {}

    async def fake(**kwargs: object) -> object:
        seen.update(kwargs)
        return await litellm.acompletion(**kwargs, mock_response="ok")  # type: ignore[arg-type]

    settings = FAKE.model_copy(update={"request_timeout": 7.0, "max_retries": 1})
    await ProviderRouter(settings, completion_fn=fake).complete(MESSAGES, model="gemini")
    assert seen["timeout"] == 7.0 and seen["num_retries"] == 1
    await ProviderRouter(settings, completion_fn=fake).complete(MESSAGES, model="gemini", timeout=3)
    assert seen["timeout"] == 3  # per-call override wins


GEMINI_503 = (
    'GeminiException - {\n  "error": {\n    "code": 503,\n    "message": "This model is '
    'currently experiencing high demand.",\n    "status": "UNAVAILABLE"\n  }\n}\n'
)
ANTHROPIC_400 = (
    'AnthropicException - {"type":"error","error":{"type":"invalid_request_error",'
    '"message":"Your credit balance is too low to access the Anthropic API."}}'
)


@pytest.mark.parametrize(
    ("exc", "alias", "expected"),
    [
        (
            litellm.ServiceUnavailableError(GEMINI_503, "gemini", "gemini/x"),
            "gemini",
            "Gemini is temporarily unavailable: This model is currently experiencing high demand.",
        ),
        (
            litellm.BadRequestError(ANTHROPIC_400, "anthropic/x", "anthropic"),
            "claude",
            "Claude request failed: Your credit balance is too low to access the Anthropic API.",
        ),
        (
            litellm.AuthenticationError("bad key", "anthropic", "anthropic/x"),
            "claude",
            "Claude rejected the API key",
        ),
        (
            litellm.NotFoundError('{"error":{"message":"gone"}}', "gemini/x", "gemini"),
            "gemini",
            "is not available: gone",
        ),
        (litellm.Timeout("slow", "gemini/x", "gemini"), "gemini", "did not respond in time"),
    ],
)
@pytest.mark.parametrize("mode", ["complete", "stream"])
async def test_provider_errors_are_concise(
    exc: Exception, alias: str, expected: str, mode: str
) -> None:
    async def failing(**_: object) -> object:
        raise exc

    router = ProviderRouter(FAKE, completion_fn=failing)
    with pytest.raises(ProviderError) as info:
        if mode == "complete":
            await router.complete(MESSAGES, model=alias)
        else:
            async for _ in router.stream(MESSAGES, model=alias):
                pass
    assert expected in str(info.value)
    assert "\n" not in str(info.value) and "{" not in str(info.value)
    assert info.value.__cause__ is exc


async def test_mid_stream_failure_becomes_provider_error() -> None:
    class Broken:
        def __aiter__(self) -> "Broken":
            return self

        async def __anext__(self) -> object:
            raise litellm.ServiceUnavailableError(GEMINI_503, "gemini", "gemini/x")

    async def fake(**_: object) -> object:
        return Broken()

    with pytest.raises(ProviderError, match="temporarily unavailable"):
        async for _ in ProviderRouter(FAKE, completion_fn=fake).stream(MESSAGES, model="gemini"):
            pass


# ---------- integration ----------

LIVE = load_settings()


def _live_params(alias: str) -> object:
    key = LIVE.anthropic_api_key if alias == "claude" else LIVE.gemini_api_key
    return pytest.param(
        alias,
        marks=[
            pytest.mark.integration,
            pytest.mark.skipif(key is None, reason=f"no API key for {alias}"),
        ],
    )


@pytest.mark.parametrize("alias", [_live_params("claude"), _live_params("gemini")])
async def test_live_complete(alias: str) -> None:
    result = await ProviderRouter(LIVE).complete(MESSAGES, model=alias, max_tokens=256)
    assert isinstance(result, CompletionResult)
    assert "pong" in result.content.lower()
    assert result.usage.total_tokens > 0


@pytest.mark.parametrize("alias", [_live_params("claude"), _live_params("gemini")])
async def test_live_stream(alias: str) -> None:
    chunks = [c async for c in ProviderRouter(LIVE).stream(MESSAGES, model=alias, max_tokens=256)]
    assert "pong" in "".join(c.delta for c in chunks).lower()
