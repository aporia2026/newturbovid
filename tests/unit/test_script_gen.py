"""Tests for the script generator.

Covers:
  - OVERRIDE mode short-circuits (no LLM call, used_override=True, zero cost)
  - TONE mode includes tone_hints in the user message
  - DIRECTIVE mode includes directives + emphasises they MUST be honored
  - MIXED mode includes both
  - NONE mode runs LLM with article only
  - Article body is truncated for the prompt
  - Empty/missing article doesn't break the call
  - Malformed JSON response salvages the raw text as script
  - Empty script in JSON gets a generic fallback
  - Style direction defaults when missing
  - Cost propagates from OpenAI to ScriptResult
  - Word count is computed
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.pipeline.open_comments import OpenCommentsAnalysis, OpenCommentsMode
from bulkvid.pipeline.script_gen import (
    ARTICLE_PROMPT_CHARS,
    DEFAULT_STYLE_DIRECTION,
    DEFAULT_TARGET_WORDS,
    ScriptResult,
    generate_script,
)

API_KEY = "sk-test"
BASE = "https://api.openai.com/v1"


def _chat_response(content: str, ptokens: int = 200, ctokens: int = 80) -> dict:
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


def _capture_handler(captured: list[dict]):
    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_chat_response(
                json.dumps(
                    {
                        "script": "Discover the best deal of the day.",
                        "style_direction": "Warm and confident.",
                    }
                )
            ),
        )

    return _handler


# ── OVERRIDE short-circuit ──────────────────────────────────────────────────


@respx.mock
async def test_override_mode_returns_user_script_without_llm_call() -> None:
    override_text = (
        "Looking for the best smartwatch this year? "
        "Check out our top three picks under two hundred dollars."
    )
    open_comments = OpenCommentsAnalysis(
        mode=OpenCommentsMode.OVERRIDE,
        raw_text=override_text,
        override_script=override_text,
    )

    async with OpenAIClient(api_key=API_KEY) as client:
        result = await generate_script(
            client,
            article_body="should be ignored",
            country="US",
            vertical="tech",
            language="en",
            script_pattern="How To",
            open_comments=open_comments,
        )

    assert result.used_override is True
    assert result.script == override_text
    assert result.style_direction == DEFAULT_STYLE_DIRECTION
    assert result.cost_usd == 0.0
    assert result.word_count == len(override_text.split())


@respx.mock
async def test_override_mode_falls_through_when_override_script_missing() -> None:
    # Mode says OVERRIDE but override_script is None — fall through to LLM path.
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(side_effect=_capture_handler(captured))

    open_comments = OpenCommentsAnalysis(
        mode=OpenCommentsMode.OVERRIDE,
        raw_text="oops the parser dropped the script",
        override_script=None,
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        result = await generate_script(
            client,
            article_body="some article body",
            country="US",
            vertical="tech",
            language="en",
            script_pattern="How To",
            open_comments=open_comments,
        )

    assert result.used_override is False
    assert len(captured) == 1   # LLM was called


# ── TONE / DIRECTIVE / MIXED routing into the prompt ────────────────────────


@respx.mock
async def test_tone_mode_includes_tone_hints_in_user_message() -> None:
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(side_effect=_capture_handler(captured))

    open_comments = OpenCommentsAnalysis(
        mode=OpenCommentsMode.TONE,
        raw_text="urgent",
        tone_hints=["urgent", "high energy"],
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        await generate_script(
            client,
            article_body="Article about smartwatches.",
            country="US",
            vertical="tech",
            language="en",
            script_pattern="How To",
            open_comments=open_comments,
        )

    user_msg = captured[0]["messages"][1]["content"]
    assert "TONE_HINTS" in user_msg
    assert "urgent" in user_msg
    assert "high energy" in user_msg


@respx.mock
async def test_directive_mode_emphasises_must_honor() -> None:
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(side_effect=_capture_handler(captured))

    open_comments = OpenCommentsAnalysis(
        mode=OpenCommentsMode.DIRECTIVE,
        raw_text="must mention $9.99",
        directives=["mention $9.99", "CTA: Learn More"],
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        await generate_script(
            client,
            article_body="x",
            country="US",
            vertical="tech",
            language="en",
            script_pattern="How To",
            open_comments=open_comments,
        )

    user_msg = captured[0]["messages"][1]["content"]
    assert "DIRECTIVES" in user_msg
    assert "mention $9.99" in user_msg
    assert "CTA: Learn More" in user_msg

    system_msg = captured[0]["messages"][0]["content"]
    # The system prompt always communicates that directives are non-negotiable.
    assert "DIRECTIVES" in system_msg
    assert "MUST" in system_msg or "must" in system_msg


@respx.mock
async def test_mixed_mode_includes_both_tone_and_directives() -> None:
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(side_effect=_capture_handler(captured))

    open_comments = OpenCommentsAnalysis(
        mode=OpenCommentsMode.MIXED,
        raw_text="urgent, mention $9.99",
        tone_hints=["urgent"],
        directives=["mention $9.99"],
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        await generate_script(
            client,
            article_body="x",
            country="US",
            vertical="tech",
            language="en",
            script_pattern="How To",
            open_comments=open_comments,
        )

    user_msg = captured[0]["messages"][1]["content"]
    assert "TONE_HINTS" in user_msg
    assert "DIRECTIVES" in user_msg


@respx.mock
async def test_none_mode_runs_llm_without_tone_or_directives_block() -> None:
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(side_effect=_capture_handler(captured))

    open_comments = OpenCommentsAnalysis(mode=OpenCommentsMode.NONE, raw_text="")
    async with OpenAIClient(api_key=API_KEY) as client:
        await generate_script(
            client,
            article_body="Article about smartwatches.",
            country="US",
            vertical="tech",
            language="en",
            script_pattern="How To",
            open_comments=open_comments,
        )

    user_msg = captured[0]["messages"][1]["content"]
    assert "TONE_HINTS" not in user_msg
    assert "DIRECTIVES" not in user_msg
    assert "ARTICLE BODY" in user_msg


# ── Article handling ───────────────────────────────────────────────────────


@respx.mock
async def test_article_body_is_truncated_in_prompt() -> None:
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(side_effect=_capture_handler(captured))

    long_article = "X" * (ARTICLE_PROMPT_CHARS * 5)  # 5x the cap

    async with OpenAIClient(api_key=API_KEY) as client:
        await generate_script(
            client,
            article_body=long_article,
            country="US",
            vertical="tech",
            language="en",
            script_pattern="How To",
            open_comments=OpenCommentsAnalysis(mode=OpenCommentsMode.NONE, raw_text=""),
        )

    user_msg = captured[0]["messages"][1]["content"]
    # The "X" content in the user message is bounded by ARTICLE_PROMPT_CHARS.
    x_count = user_msg.count("X")
    assert x_count <= ARTICLE_PROMPT_CHARS


@respx.mock
async def test_empty_article_does_not_break_generation() -> None:
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(side_effect=_capture_handler(captured))

    async with OpenAIClient(api_key=API_KEY) as client:
        result = await generate_script(
            client,
            article_body="",
            country="US",
            vertical="tech",
            language="en",
            script_pattern="How To",
            open_comments=OpenCommentsAnalysis(mode=OpenCommentsMode.NONE, raw_text=""),
        )

    user_msg = captured[0]["messages"][1]["content"]
    assert "ARTICLE BODY" in user_msg
    assert isinstance(result, ScriptResult)


# ── Robustness ─────────────────────────────────────────────────────────────


@respx.mock
async def test_malformed_json_falls_back_to_raw_text_as_script() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_chat_response("Hello this is just plain text not json.")
        )
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        result = await generate_script(
            client,
            article_body="x",
            country="US",
            vertical="tech",
            language="en",
            script_pattern="How To",
            open_comments=OpenCommentsAnalysis(mode=OpenCommentsMode.NONE, raw_text=""),
        )
    assert "Hello this is just plain text" in result.script
    assert result.style_direction == DEFAULT_STYLE_DIRECTION
    assert result.used_override is False
    assert result.cost_usd > 0


@respx.mock
async def test_empty_script_field_gets_generic_fallback() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_chat_response(
                json.dumps({"script": "", "style_direction": "Warm."})
            ),
        )
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        result = await generate_script(
            client,
            article_body="x",
            country="US",
            vertical="tech",
            language="en",
            script_pattern="How To",
            open_comments=OpenCommentsAnalysis(mode=OpenCommentsMode.NONE, raw_text=""),
        )
    assert result.script != ""
    assert "tech" in result.script.lower()   # fallback weaves the vertical in


@respx.mock
async def test_missing_style_direction_uses_default() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_chat_response(json.dumps({"script": "Hello there friends."}))
        )
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        result = await generate_script(
            client,
            article_body="x",
            country="US",
            vertical="tech",
            language="en",
            script_pattern="How To",
            open_comments=OpenCommentsAnalysis(mode=OpenCommentsMode.NONE, raw_text=""),
        )
    assert result.style_direction == DEFAULT_STYLE_DIRECTION


# ── Word count + result type ───────────────────────────────────────────────


@respx.mock
async def test_word_count_is_computed() -> None:
    script = "One two three four five six seven eight nine ten."
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_chat_response(
                json.dumps({"script": script, "style_direction": "Confident."})
            ),
        )
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        result = await generate_script(
            client,
            article_body="x",
            country="US",
            vertical="tech",
            language="en",
            script_pattern="How To",
            open_comments=OpenCommentsAnalysis(mode=OpenCommentsMode.NONE, raw_text=""),
        )
    assert result.word_count == 10


def test_default_target_words_sensible() -> None:
    # Target ~10-12s spoken ≈ ~15-20 words at the observed (~1.5 words/sec) TTS rate.
    assert 14 <= DEFAULT_TARGET_WORDS <= 24
