"""Tests for the OpenAI adapter.

OpenAI's AsyncOpenAI uses httpx under the hood, so we mock at the httpx layer
via respx — no real OpenAI calls.

Covers:
  - estimate_cost_usd for each priced model + unknown model fallback
  - chat() success returns ChatResult with cost
  - chat() with response_format passes through
  - vision_describe with image_url and image_b64
  - vision_describe rejects when neither image source provided
  - 401 -> OpenAIAuthError (terminal, no retry)
  - 400 -> OpenAIError (terminal, no retry)
  - 429 -> retried, then OpenAIRateLimitError after exhausting attempts
  - 429 -> 200 succeeds after one retry
  - 500 -> retried as OpenAIServerError
  - Retry-After header honored
  - Constructor rejects empty api_key
"""

from __future__ import annotations

import httpx
import pytest
import respx

from bulkvid.adapters import _retry
from bulkvid.adapters.openai_client import (
    MODEL_SCRIPT_GEN,
    MODEL_VISION,
    PRICING_PER_1M_TOKENS,
    OpenAIAuthError,
    OpenAIClient,
    OpenAIError,
    OpenAIRateLimitError,
    OpenAIServerError,
    estimate_cost_usd,
)


@pytest.fixture(autouse=True)
def _no_sleep_between_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every retry sleep instant so the suite stays fast."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_retry.asyncio, "sleep", _instant)

API_KEY = "sk-test-openai"
BASE = "https://api.openai.com/v1"


def _chat_response(text: str, model: str, ptokens: int = 100, ctokens: int = 50) -> dict:
    """Build an OpenAI-shaped chat.completion response."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1717_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": ptokens,
            "completion_tokens": ctokens,
            "total_tokens": ptokens + ctokens,
        },
    }


# ── estimate_cost_usd ───────────────────────────────────────────────────────


def test_pricing_table_has_required_models() -> None:
    for model in ("gpt-5.4-mini", "gpt-4o"):
        assert model in PRICING_PER_1M_TOKENS
        assert PRICING_PER_1M_TOKENS[model]["input"] > 0
        assert PRICING_PER_1M_TOKENS[model]["output"] > 0


def test_estimate_cost_gpt_5_4_mini() -> None:
    # 1M input tokens * $0.75 + 1M output * $4.50 = $5.25
    cost = estimate_cost_usd("gpt-5.4-mini", 1_000_000, 1_000_000)
    assert cost == pytest.approx(5.25, abs=1e-6)


def test_estimate_cost_gpt_4o() -> None:
    # 100k input * $2.50/M + 200k output * $10/M
    cost = estimate_cost_usd("gpt-4o", 100_000, 200_000)
    expected = (100_000 / 1e6 * 2.50) + (200_000 / 1e6 * 10.00)
    assert cost == pytest.approx(expected, abs=1e-6)


def test_estimate_cost_zero_tokens() -> None:
    assert estimate_cost_usd("gpt-5.4-mini", 0, 0) == 0.0


def test_estimate_cost_unknown_model_returns_zero() -> None:
    # Unknown models are logged but don't crash the pipeline.
    assert estimate_cost_usd("gpt-future-9000", 1000, 500) == 0.0


# ── Constructor ─────────────────────────────────────────────────────────────


def test_constructor_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError):
        OpenAIClient(api_key="")


# ── chat() ──────────────────────────────────────────────────────────────────


@respx.mock
async def test_chat_success_returns_text_and_cost() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_chat_response("hello world", MODEL_SCRIPT_GEN, ptokens=200, ctokens=100),
        )
    )

    async with OpenAIClient(api_key=API_KEY) as client:
        result = await client.chat(
            model=MODEL_SCRIPT_GEN,
            messages=[{"role": "user", "content": "hi"}],
        )

    assert result.text == "hello world"
    assert result.prompt_tokens == 200
    assert result.completion_tokens == 100
    assert result.model == MODEL_SCRIPT_GEN
    # Cost: 200 input * 0.75/M + 100 output * 4.50/M
    expected_cost = (200 / 1e6 * 0.75) + (100 / 1e6 * 4.50)
    assert result.cost_usd == pytest.approx(expected_cost, abs=1e-6)


@respx.mock
async def test_chat_passes_response_format() -> None:
    captured: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.append(json.loads(request.content))
        return httpx.Response(
            200, json=_chat_response('{"k":"v"}', MODEL_SCRIPT_GEN)
        )

    respx.post(f"{BASE}/chat/completions").mock(side_effect=_handler)

    async with OpenAIClient(api_key=API_KEY) as client:
        await client.chat(
            model=MODEL_SCRIPT_GEN,
            messages=[{"role": "user", "content": "x"}],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=500,
        )

    body = captured[0]
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0.2
    # OpenAI deprecated max_tokens in favour of max_completion_tokens.
    # The adapter accepts the old name as a Python kwarg but sends the new
    # name on the wire.
    assert body["max_completion_tokens"] == 500
    assert "max_tokens" not in body


@respx.mock
async def test_chat_401_raises_auth_error() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            401, json={"error": {"message": "Invalid API key", "type": "invalid_request_error"}}
        )
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        with pytest.raises(OpenAIAuthError):
            await client.chat(
                model=MODEL_SCRIPT_GEN,
                messages=[{"role": "user", "content": "x"}],
            )


@respx.mock
async def test_chat_429_raises_rate_limit_error() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            429,
            json={"error": {"message": "rate limited", "type": "rate_limit_error"}},
        )
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        with pytest.raises(OpenAIRateLimitError):
            await client.chat(
                model=MODEL_SCRIPT_GEN,
                messages=[{"role": "user", "content": "x"}],
            )


# ── vision_describe ─────────────────────────────────────────────────────────


@respx.mock
async def test_vision_describe_with_image_url() -> None:
    captured: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.append(json.loads(request.content))
        return httpx.Response(
            200, json=_chat_response("a sunset over the ocean", MODEL_VISION)
        )

    respx.post(f"{BASE}/chat/completions").mock(side_effect=_handler)

    async with OpenAIClient(api_key=API_KEY) as client:
        result = await client.vision_describe(
            prompt="describe",
            image_url="https://example.com/img.png",
        )

    assert "sunset" in result.text
    # Sent content must be a list with text+image_url parts.
    content = captured[0]["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == "https://example.com/img.png"
    assert content[1]["image_url"]["detail"] == "high"


@respx.mock
async def test_vision_describe_with_image_b64() -> None:
    captured: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response("a dog", MODEL_VISION))

    respx.post(f"{BASE}/chat/completions").mock(side_effect=_handler)

    async with OpenAIClient(api_key=API_KEY) as client:
        await client.vision_describe(prompt="describe", image_b64="ABCDEFG=")

    content = captured[0]["messages"][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,ABCDEFG")


async def test_vision_describe_requires_image_source() -> None:
    async with OpenAIClient(api_key=API_KEY) as client:
        with pytest.raises(ValueError):
            await client.vision_describe(prompt="x")


# ── Retry behavior ──────────────────────────────────────────────────────────


@respx.mock
async def test_chat_429_then_200_succeeds_after_one_retry() -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(429, json={"error": {"message": "slow down", "type": "rate_limit_error"}}),
            httpx.Response(200, json=_chat_response("ok", MODEL_SCRIPT_GEN)),
        ]
    )

    async with OpenAIClient(api_key=API_KEY) as client:
        result = await client.chat(
            model=MODEL_SCRIPT_GEN,
            messages=[{"role": "user", "content": "hi"}],
        )

    assert result.text == "ok"
    assert route.call_count == 2


@respx.mock
async def test_chat_429_exhausts_then_raises_rate_limit() -> None:
    # Persistent 429 — the helper must give up after 3 attempts and raise the
    # original error class so callers' except blocks still match.
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            429,
            json={"error": {"message": "rate limited", "type": "rate_limit_error"}},
        )
    )

    async with OpenAIClient(api_key=API_KEY) as client:
        with pytest.raises(OpenAIRateLimitError):
            await client.chat(
                model=MODEL_SCRIPT_GEN,
                messages=[{"role": "user", "content": "x"}],
            )

    assert route.call_count == 3


@respx.mock
async def test_chat_500_is_retried_as_server_error() -> None:
    # InternalServerError is retryable; after 3 failures, raise OpenAIServerError.
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            500,
            json={"error": {"message": "boom", "type": "server_error"}},
        )
    )

    async with OpenAIClient(api_key=API_KEY) as client:
        with pytest.raises(OpenAIServerError):
            await client.chat(
                model=MODEL_SCRIPT_GEN,
                messages=[{"role": "user", "content": "x"}],
            )

    assert route.call_count == 3


@respx.mock
async def test_chat_400_does_not_retry() -> None:
    # Bad request is terminal — retrying a malformed prompt just wastes budget.
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            400,
            json={"error": {"message": "bad prompt", "type": "invalid_request_error"}},
        )
    )

    async with OpenAIClient(api_key=API_KEY) as client:
        with pytest.raises(OpenAIError):
            await client.chat(
                model=MODEL_SCRIPT_GEN,
                messages=[{"role": "user", "content": "x"}],
            )

    assert route.call_count == 1


@respx.mock
async def test_chat_401_does_not_retry() -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            401, json={"error": {"message": "no key", "type": "invalid_request_error"}}
        )
    )

    async with OpenAIClient(api_key=API_KEY) as client:
        with pytest.raises(OpenAIAuthError):
            await client.chat(
                model=MODEL_SCRIPT_GEN,
                messages=[{"role": "user", "content": "x"}],
            )

    assert route.call_count == 1


@respx.mock
async def test_chat_honors_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Capture every requested sleep duration so we can assert the value the
    # provider asked for is what we waited.
    waits: list[float] = []

    async def capture_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(_retry.asyncio, "sleep", capture_sleep)

    respx.post(f"{BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(
                429,
                json={"error": {"message": "wait", "type": "rate_limit_error"}},
                headers={"retry-after": "4"},
            ),
            httpx.Response(200, json=_chat_response("ok", MODEL_SCRIPT_GEN)),
        ]
    )

    async with OpenAIClient(api_key=API_KEY) as client:
        result = await client.chat(
            model=MODEL_SCRIPT_GEN,
            messages=[{"role": "user", "content": "x"}],
        )

    assert result.text == "ok"
    # Retry-After: 4 takes precedence over jittered exponential backoff.
    assert waits == [4.0]


def test_max_retries_disabled_on_underlying_sdk_client() -> None:
    # If we ever forget to disable the SDK's internal retry, our wrapper
    # produces 3×3 = 9 round trips with silent latency. Pin the behavior.
    client = OpenAIClient(api_key=API_KEY)
    assert client._client.max_retries == 0    # type: ignore[attr-defined]
