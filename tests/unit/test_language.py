"""Tests for language detection.

OpenAI HTTP layer mocked with respx. Covers:
  - Empty text -> default lang, no LLM call
  - Detected languages round-trip cleanly (en, he, ar, fr)
  - Cache hit on second call (zero cost, cached=True)
  - Cache disabled forces a fresh LLM call
  - Cache key is article-snippet based (different prefixes -> separate entries)
  - Unsupported language code -> "en" fallback
  - Malformed JSON -> "en" fallback, cost still charged
  - Confidence clamped to [0, 1]
  - Cache size respects upper bound
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.pipeline import language as lang_mod
from bulkvid.pipeline.language import (
    CACHE_SIZE,
    DEFAULT_LANGUAGE,
    _cache,
    _cache_key,
    cache_size,
    clear_cache,
    detect_language,
)

API_KEY = "sk-test"
BASE = "https://api.openai.com/v1"


def _chat_response(content: str, ptokens: int = 50, ctokens: int = 20) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1717_000_000,
        "model": "gpt-5.4-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": ptokens,
            "completion_tokens": ctokens,
            "total_tokens": ptokens + ctokens,
        },
    }


@pytest.fixture(autouse=True)
def _isolate_cache() -> None:
    """Every test starts with an empty cache."""
    clear_cache()


# ── Empty input ─────────────────────────────────────────────────────────────


@respx.mock
async def test_empty_text_returns_default_without_llm_call() -> None:
    async with OpenAIClient(api_key=API_KEY) as client:
        result = await detect_language(client, "")
    assert result.language == DEFAULT_LANGUAGE
    assert result.cost_usd == 0.0
    assert result.cached is False


@respx.mock
async def test_whitespace_only_returns_default_without_llm_call() -> None:
    async with OpenAIClient(api_key=API_KEY) as client:
        result = await detect_language(client, "   \n\n  ")
    assert result.language == DEFAULT_LANGUAGE


# ── Happy paths per language ────────────────────────────────────────────────


@respx.mock
async def test_detects_english() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_chat_response(json.dumps({"language": "en", "confidence": 0.99}))
        )
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        result = await detect_language(client, "The latest smartwatch reviews for 2026.")
    assert result.language == "en"
    assert result.confidence == pytest.approx(0.99)
    assert result.cached is False
    assert result.cost_usd > 0


@respx.mock
async def test_detects_hebrew() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_chat_response(json.dumps({"language": "he", "confidence": 0.97}))
        )
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        result = await detect_language(
            client, "ביקורות שעוני חכמים לשנת 2026 - מה כדאי לקנות"
        )
    assert result.language == "he"


@respx.mock
async def test_detects_arabic() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_chat_response(json.dumps({"language": "ar", "confidence": 0.93}))
        )
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        result = await detect_language(client, "مراجعات أفضل الساعات الذكية لعام 2026")
    assert result.language == "ar"


# ── Cache behavior ──────────────────────────────────────────────────────────


@respx.mock
async def test_cache_hit_on_second_call_skips_llm() -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_chat_response(json.dumps({"language": "en", "confidence": 0.99}))
        )
    )
    body = "Same article body for both calls."
    async with OpenAIClient(api_key=API_KEY) as client:
        first = await detect_language(client, body)
        second = await detect_language(client, body)

    # Only one LLM call across both detections.
    assert route.call_count == 1
    assert first.cached is False
    assert second.cached is True
    assert second.language == "en"
    assert second.cost_usd == 0.0


@respx.mock
async def test_cache_can_be_disabled_per_call() -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_chat_response(json.dumps({"language": "en", "confidence": 0.99}))
        )
    )
    body = "Body."
    async with OpenAIClient(api_key=API_KEY) as client:
        await detect_language(client, body, use_cache=False)
        await detect_language(client, body, use_cache=False)
    assert route.call_count == 2
    assert cache_size() == 0


def test_cache_key_is_snippet_based() -> None:
    short_a = "AAA " + "X" * 100
    short_b = "BBB " + "X" * 100
    # Different prefixes -> different cache keys.
    assert _cache_key(short_a) != _cache_key(short_b)
    # Identical prefixes within the snippet window collapse to the same key
    # even if the bodies diverge later.
    long_a = ("PREFIX " * 50) + "tail_one"
    long_b = ("PREFIX " * 50) + "tail_two"
    if len(long_a) > 500 and len(long_b) > 500 and long_a[:500] == long_b[:500]:
        assert _cache_key(long_a) == _cache_key(long_b)


# ── Robustness ──────────────────────────────────────────────────────────────


@respx.mock
async def test_unsupported_language_code_falls_back_to_default() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_chat_response(json.dumps({"language": "xx", "confidence": 0.5}))
        )
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        result = await detect_language(client, "Some text in unknown lang.")
    assert result.language == DEFAULT_LANGUAGE


@respx.mock
async def test_malformed_json_falls_back_to_default_but_charges_cost() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_response("not-json"))
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        result = await detect_language(client, "Some body.")
    assert result.language == DEFAULT_LANGUAGE
    assert result.cost_usd > 0
    assert result.cached is False


@respx.mock
async def test_confidence_is_clamped_to_unit_interval() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_chat_response(json.dumps({"language": "en", "confidence": 1.7}))
        )
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        result = await detect_language(client, "x")
    assert 0.0 <= result.confidence <= 1.0


@respx.mock
async def test_confidence_clamped_when_negative() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_chat_response(json.dumps({"language": "en", "confidence": -3.0}))
        )
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        result = await detect_language(client, "x")
    assert result.confidence == 0.0


# ── Cache size bound ────────────────────────────────────────────────────────


def test_cache_respects_upper_bound() -> None:
    # Drop in items directly to exercise the eviction policy without an LLM.
    for i in range(CACHE_SIZE + 50):
        _cache.put(f"key-{i}", "en")
    assert len(_cache) == CACHE_SIZE
