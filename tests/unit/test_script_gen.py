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


# ── Template selector integration ───────────────────────────────────────────
#
# When script_pattern is blank AND the settings store provides a library +
# enabled flag, the selector runs and its chosen body becomes the effective
# script_pattern.


class _FakeSettingsStore:
    """Async-shaped settings store stub.

    Real ``SettingsStore`` needs SQLite + Turso setup; this stub returns
    canned values for the keys script_gen consults.
    """

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    async def get(self, key: str, default: str | None = None) -> str:
        return self._values.get(key, default if default is not None else "")


@respx.mock
async def test_blank_script_pattern_falls_back_to_literal_without_settings_store() -> None:
    """Without a settings store the selector cannot run, and the existing
    literal "natural conversational opener" fallback in _format_system_prompt
    kicks in. Pinned so a future settings-store wiring change doesn't silently
    regress this contract."""
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(side_effect=_capture_handler(captured))

    async with OpenAIClient(api_key=API_KEY) as client:
        await generate_script(
            client,
            article_body="An article body here.",
            country="US",
            vertical="tech",
            language="en",
            script_pattern="",
            open_comments=OpenCommentsAnalysis(mode=OpenCommentsMode.NONE, raw_text=""),
            settings_store=None,
        )

    system_msg = captured[0]["messages"][0]["content"]
    assert "natural conversational opener" in system_msg


@respx.mock
async def test_blank_script_pattern_uses_selected_template_body() -> None:
    """Blank cell + selector enabled → the template body is substituted into
    the system prompt as the SCRIPT PATTERN, and the chosen id is returned
    on the ScriptResult so the sidebar can show it."""
    from bulkvid.orchestrator.runtime_settings import (
        SETTING_SCRIPT_TEMPLATE_LIBRARY,
        SETTING_TEMPLATE_SELECTOR_ENABLED,
    )

    library_json = json.dumps(
        {
            "version": 1,
            "templates": [
                {
                    "id": "tone_a",
                    "name": "A",
                    "hint": "warm",
                    "body": "BODY_FOR_A",
                },
                {
                    "id": "tone_b",
                    "name": "B",
                    "hint": "punchy",
                    "body": "BODY_FOR_B",
                },
            ],
        }
    )

    captured: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        # First call = selector; second call = generator.
        if any("routing assistant" in m.get("content", "") for m in body["messages"]):
            return httpx.Response(
                200,
                json=_chat_response(
                    json.dumps({"template_id": "tone_b", "reason": "punchy fits"})
                ),
            )
        return httpx.Response(
            200,
            json=_chat_response(
                json.dumps({"script": "Some 10 second script.",
                            "style_direction": "Warm."})
            ),
        )

    respx.post(f"{BASE}/chat/completions").mock(side_effect=_handler)

    store = _FakeSettingsStore(
        {
            SETTING_TEMPLATE_SELECTOR_ENABLED: "true",
            SETTING_SCRIPT_TEMPLATE_LIBRARY: library_json,
        }
    )

    async with OpenAIClient(api_key=API_KEY) as client:
        result = await generate_script(
            client,
            article_body="Some article body content.",
            country="US",
            vertical="news",
            language="en",
            script_pattern="",
            open_comments=OpenCommentsAnalysis(mode=OpenCommentsMode.NONE, raw_text=""),
            settings_store=store,    # type: ignore[arg-type]
        )

    # Two OpenAI calls were made: the selector and the script gen.
    assert len(captured) == 2
    # The selected template body shows up in the final system prompt.
    final_system = captured[-1]["messages"][0]["content"]
    assert "BODY_FOR_B" in final_system
    # The chosen id rides back on the result so the sidebar can render it.
    assert result.chosen_template_id == "tone_b"


@respx.mock
async def test_filled_script_pattern_leaves_chosen_template_id_empty() -> None:
    """A row that supplied its own pattern never engages the selector, so
    the result's chosen_template_id stays empty (sidebar shows nothing)."""
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_chat_response(
                json.dumps({"script": "x", "style_direction": "Warm."})
            ),
        )
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        result = await generate_script(
            client,
            article_body="x",
            country="US", vertical="tech", language="en",
            script_pattern="How To",
            open_comments=OpenCommentsAnalysis(mode=OpenCommentsMode.NONE, raw_text=""),
        )
    assert result.chosen_template_id == ""


@respx.mock
async def test_blank_script_pattern_with_selector_disabled_uses_literal() -> None:
    """Master switch ``template_selector_enabled=false`` reverts to the literal
    fallback even when a library is configured."""
    from bulkvid.orchestrator.runtime_settings import (
        SETTING_SCRIPT_TEMPLATE_LIBRARY,
        SETTING_TEMPLATE_SELECTOR_ENABLED,
    )

    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(side_effect=_capture_handler(captured))

    library_json = json.dumps(
        {"version": 1, "templates": [
            {"id": "x", "name": "X", "hint": "h", "body": "BODY_FOR_X"},
            {"id": "y", "name": "Y", "hint": "h", "body": "BODY_FOR_Y"},
        ]}
    )
    store = _FakeSettingsStore(
        {
            SETTING_TEMPLATE_SELECTOR_ENABLED: "false",
            SETTING_SCRIPT_TEMPLATE_LIBRARY: library_json,
        }
    )

    async with OpenAIClient(api_key=API_KEY) as client:
        await generate_script(
            client,
            article_body="body",
            country="US",
            vertical="news",
            language="en",
            script_pattern="",
            open_comments=OpenCommentsAnalysis(mode=OpenCommentsMode.NONE, raw_text=""),
            settings_store=store,    # type: ignore[arg-type]
        )

    # Only one call (the script gen), no selector call.
    assert len(captured) == 1
    system_msg = captured[0]["messages"][0]["content"]
    assert "natural conversational opener" in system_msg
    assert "BODY_FOR_" not in system_msg


@respx.mock
async def test_non_blank_script_pattern_skips_selector() -> None:
    """Existing behavior must not regress: a filled script_pattern column never
    triggers the selector."""
    from bulkvid.orchestrator.runtime_settings import (
        SETTING_SCRIPT_TEMPLATE_LIBRARY,
        SETTING_TEMPLATE_SELECTOR_ENABLED,
    )

    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(side_effect=_capture_handler(captured))

    library_json = json.dumps(
        {"version": 1, "templates": [
            {"id": "x", "name": "X", "hint": "h", "body": "BODY_FOR_X"},
            {"id": "y", "name": "Y", "hint": "h", "body": "BODY_FOR_Y"},
        ]}
    )
    store = _FakeSettingsStore(
        {
            SETTING_TEMPLATE_SELECTOR_ENABLED: "true",
            SETTING_SCRIPT_TEMPLATE_LIBRARY: library_json,
        }
    )

    async with OpenAIClient(api_key=API_KEY) as client:
        await generate_script(
            client,
            article_body="body",
            country="US",
            vertical="news",
            language="en",
            script_pattern="How To",
            open_comments=OpenCommentsAnalysis(mode=OpenCommentsMode.NONE, raw_text=""),
            settings_store=store,    # type: ignore[arg-type]
        )

    # Only one call — no selector hop.
    assert len(captured) == 1
    system_msg = captured[0]["messages"][0]["content"]
    assert "How To" in system_msg
    assert "BODY_FOR_" not in system_msg


@respx.mock
async def test_selector_failure_falls_back_to_literal() -> None:
    """If the selector returns invalid JSON, generation proceeds with the
    literal default — never blocks the row."""
    from bulkvid.orchestrator.runtime_settings import (
        SETTING_SCRIPT_TEMPLATE_LIBRARY,
        SETTING_TEMPLATE_SELECTOR_ENABLED,
    )

    library_json = json.dumps(
        {"version": 1, "templates": [
            {"id": "x", "name": "X", "hint": "h", "body": "BODY_FOR_X"},
            {"id": "y", "name": "Y", "hint": "h", "body": "BODY_FOR_Y"},
        ]}
    )

    captured: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if any("routing assistant" in m.get("content", "") for m in body["messages"]):
            # Selector returns garbage.
            return httpx.Response(
                200, json=_chat_response("not actually json")
            )
        return httpx.Response(
            200,
            json=_chat_response(
                json.dumps({"script": "ok", "style_direction": "Warm."})
            ),
        )

    respx.post(f"{BASE}/chat/completions").mock(side_effect=_handler)

    store = _FakeSettingsStore(
        {
            SETTING_TEMPLATE_SELECTOR_ENABLED: "true",
            SETTING_SCRIPT_TEMPLATE_LIBRARY: library_json,
        }
    )

    async with OpenAIClient(api_key=API_KEY) as client:
        result = await generate_script(
            client,
            article_body="body",
            country="US",
            vertical="news",
            language="en",
            script_pattern="",
            open_comments=OpenCommentsAnalysis(mode=OpenCommentsMode.NONE, raw_text=""),
            settings_store=store,    # type: ignore[arg-type]
        )

    # Two calls: failed selector + generator. Generator's system prompt fell
    # through to the literal default.
    assert len(captured) == 2
    final_system = captured[-1]["messages"][0]["content"]
    assert "natural conversational opener" in final_system
    assert result.script == "ok"
