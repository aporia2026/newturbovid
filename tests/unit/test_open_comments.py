"""Tests for the Open Comments classifier.

Mocks OpenAI's HTTP layer with respx — the classifier exercises the real
``OpenAIClient`` adapter but hits no network.

Covers:
  - Empty / whitespace input -> NONE without an LLM call
  - TONE / DIRECTIVE / OVERRIDE / MIXED classifications round-trip cleanly
  - Malformed JSON degrades to TONE with full text as a single hint
  - Unknown mode string degrades to TONE
  - Cost from the OpenAI call propagates onto the analysis
  - tone_hints / directives are stripped + de-empty
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.pipeline.open_comments import (
    OVERRIDE_SOFT_MAX_WORDS,
    OpenCommentsAnalysis,
    OpenCommentsMode,
    classify_open_comments,
    detect_pinned_script,
)

API_KEY = "sk-test"
BASE = "https://api.openai.com/v1"


def _chat_response(content: str, ptokens: int = 100, ctokens: int = 50) -> dict:
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


# ── Empty short-circuit ─────────────────────────────────────────────────────


@respx.mock
async def test_empty_input_returns_none_without_network_call() -> None:
    # No respx route set; if we hit OpenAI we'd raise.
    async with OpenAIClient(api_key=API_KEY) as client:
        analysis = await classify_open_comments(client, "")
    assert analysis.mode is OpenCommentsMode.NONE
    assert analysis.cost_usd == 0.0
    assert analysis.raw_text == ""


@respx.mock
async def test_whitespace_input_returns_none_without_network_call() -> None:
    async with OpenAIClient(api_key=API_KEY) as client:
        analysis = await classify_open_comments(client, "   \n  \t")
    assert analysis.mode is OpenCommentsMode.NONE


# ── Per-mode happy paths ────────────────────────────────────────────────────


@respx.mock
async def test_classifies_tone_mode() -> None:
    payload = {
        "mode": "tone",
        "tone_hints": ["urgent", "high energy"],
        "directives": [],
        "override_script": None,
    }
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_response(json.dumps(payload)))
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        analysis = await classify_open_comments(client, "urgent, high energy")

    assert analysis.mode is OpenCommentsMode.TONE
    assert analysis.tone_hints == ["urgent", "high energy"]
    assert analysis.directives == []
    assert analysis.override_script is None
    assert analysis.cost_usd > 0


@respx.mock
async def test_classifies_directive_mode() -> None:
    payload = {
        "mode": "directive",
        "tone_hints": [],
        "directives": ["mention price $9.99", "CTA: Learn More"],
        "override_script": None,
    }
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_response(json.dumps(payload)))
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        analysis = await classify_open_comments(
            client, "must mention price $9.99 and use 'Learn More' as the CTA"
        )

    assert analysis.mode is OpenCommentsMode.DIRECTIVE
    assert "mention price $9.99" in analysis.directives
    assert "CTA: Learn More" in analysis.directives
    assert analysis.override_script is None


@respx.mock
async def test_classifies_override_mode() -> None:
    script = (
        "Looking for the best smartwatch this year? "
        "Read on for our top three picks under two hundred dollars. "
        "Click the link to see the full guide."
    )
    payload = {
        "mode": "override",
        "tone_hints": [],
        "directives": [],
        "override_script": script,
    }
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_response(json.dumps(payload)))
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        analysis = await classify_open_comments(client, script)

    assert analysis.mode is OpenCommentsMode.OVERRIDE
    assert analysis.override_script == script
    assert analysis.tone_hints == []


@respx.mock
async def test_classifies_mixed_mode() -> None:
    payload = {
        "mode": "mixed",
        "tone_hints": ["urgent"],
        "directives": ["mention $9.99", "include 'free trial'"],
        "override_script": None,
    }
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_response(json.dumps(payload)))
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        analysis = await classify_open_comments(
            client,
            "make it urgent, mention $9.99 and include 'free trial' somewhere",
        )

    assert analysis.mode is OpenCommentsMode.MIXED
    assert analysis.tone_hints == ["urgent"]
    assert len(analysis.directives) == 2


# ── Robustness paths ────────────────────────────────────────────────────────


@respx.mock
async def test_malformed_json_degrades_to_tone() -> None:
    # Model returns something that is NOT valid JSON.
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_response("not-json-at-all"))
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        analysis = await classify_open_comments(client, "make it serious")

    assert analysis.mode is OpenCommentsMode.TONE
    assert analysis.tone_hints == ["make it serious"]  # raw text becomes a single hint
    # We still surface the cost — the row paid for the (broken) classifier call.
    assert analysis.cost_usd > 0


@respx.mock
async def test_unknown_mode_string_degrades_to_tone() -> None:
    payload = {
        "mode": "FUTURE_MODE_2030",
        "tone_hints": [],
        "directives": [],
        "override_script": None,
    }
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_response(json.dumps(payload)))
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        analysis = await classify_open_comments(client, "x")

    assert analysis.mode is OpenCommentsMode.TONE


@respx.mock
async def test_strips_and_drops_empty_list_items() -> None:
    payload = {
        "mode": "mixed",
        "tone_hints": ["  urgent  ", "", "calm"],
        "directives": ["", "  mention $9.99  "],
        "override_script": "   ",
    }
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_response(json.dumps(payload)))
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        analysis = await classify_open_comments(client, "x")

    assert analysis.tone_hints == ["urgent", "calm"]
    assert analysis.directives == ["mention $9.99"]
    # whitespace-only override is treated as no override.
    assert analysis.override_script is None


# ── Data class basics ───────────────────────────────────────────────────────


def test_default_analysis_has_empty_lists() -> None:
    a = OpenCommentsAnalysis(mode=OpenCommentsMode.NONE, raw_text="")
    assert a.tone_hints == []
    assert a.directives == []
    assert a.override_script is None
    assert a.cost_usd == 0.0


def test_mode_enum_string_values() -> None:
    # Values must be lower-case stable identifiers — the script generator
    # branches on them.
    assert OpenCommentsMode.NONE.value == "none"
    assert OpenCommentsMode.TONE.value == "tone"
    assert OpenCommentsMode.DIRECTIVE.value == "directive"
    assert OpenCommentsMode.OVERRIDE.value == "override"
    assert OpenCommentsMode.MIXED.value == "mixed"


# ── Pinned-script marker — pure detector ────────────────────────────────────


@pytest.mark.parametrize(
    "cell, expected",
    [
        # The manager's exact convention.
        ("use this script: Hello there friend.", "Hello there friend."),
        # Case-insensitive.
        ("USE THIS SCRIPT: Hello.", "Hello."),
        # Tolerant of the spacing a lazy operator types.
        ("  use this script:   Hello.  ", "Hello."),
        # Hyphen / dash / equals separators.
        ("use this script - Hello.", "Hello."),
        ("USE THIS SCRIPT = Hello there.", "Hello there."),
        # No separator at all, just a space.
        ("use this script Hello.", "Hello."),
        # Marker variants.
        ("use script: Hello.", "Hello."),
        ("use the script: Hello.", "Hello."),
        ("use the following script: Hello.", "Hello."),
        ("use this exact script: Hello.", "Hello."),
        # Full-width autocorrect colon.
        ("use this script：Hello.", "Hello."),  # noqa: RUF001 — testing the FW colon
        # Internal separators are preserved; only the leading run is stripped.
        ("use this script: A: B - C.", "A: B - C."),
    ],
)
def test_detect_pinned_script_matches(cell: str, expected: str) -> None:
    assert detect_pinned_script(cell) == expected


@pytest.mark.parametrize(
    "cell",
    [
        "",
        "   ",
        "make it urgent and short",                      # no marker
        "Beslagauto's in Nederland worden geveild.",     # bare paste, no marker
        "please use this script: hi",                    # marker NOT at the start
        "scripture reading for today",                   # token boundary, not a marker
        "use scripts from the library",                  # token boundary, not "use script"
        "script: be funny and upbeat",                   # bare "script" is NOT a marker
        "use this script:",                              # marker only, nothing after
        "use this script:    ",                          # marker + whitespace only
    ],
)
def test_detect_pinned_script_no_match(cell: str) -> None:
    assert detect_pinned_script(cell) is None


# ── Pinned-script marker — classifier short-circuit ─────────────────────────


@respx.mock
async def test_marker_short_circuits_without_network_call() -> None:
    # No respx route set; a network call would raise.
    async with OpenAIClient(api_key=API_KEY) as client:
        analysis = await classify_open_comments(
            client, "use this script: Buy our generic widget today, folks."
        )
    assert analysis.mode is OpenCommentsMode.OVERRIDE
    assert analysis.override_script == "Buy our generic widget today, folks."
    assert analysis.cost_usd == 0.0          # deterministic path is free
    assert analysis.override_oversize is False


@respx.mock
async def test_marker_oversize_flag_set() -> None:
    long_script = ("word " * (OVERRIDE_SOFT_MAX_WORDS + 5)) + "end."
    async with OpenAIClient(api_key=API_KEY) as client:
        analysis = await classify_open_comments(
            client, "use this script: " + long_script
        )
    assert analysis.mode is OpenCommentsMode.OVERRIDE
    assert analysis.override_oversize is True


@respx.mock
async def test_bare_paste_still_uses_llm_auto_detect() -> None:
    # A pasted script WITHOUT the marker must still reach the LLM auto-detect
    # (the locked "marker + auto-detect" behaviour) — so the route IS hit.
    script = (
        "Looking for the best smartwatch this year? "
        "Read on for our top three picks under two hundred dollars."
    )
    payload = {
        "mode": "override",
        "tone_hints": [],
        "directives": [],
        "override_script": script,
    }
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_response(json.dumps(payload)))
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        analysis = await classify_open_comments(client, script)
    assert route.called                       # the LLM path ran (not short-circuited)
    assert analysis.mode is OpenCommentsMode.OVERRIDE
    assert analysis.override_script == script
    assert analysis.cost_usd > 0


@respx.mock
async def test_marker_mid_string_falls_through_to_llm() -> None:
    # "use this script:" NOT at the start is not a pin; the LLM classifies it.
    payload = {
        "mode": "tone",
        "tone_hints": ["casual"],
        "directives": [],
        "override_script": None,
    }
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_response(json.dumps(payload)))
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        analysis = await classify_open_comments(
            client, "make it casual, and please use this script: be warm"
        )
    assert route.called
    assert analysis.mode is OpenCommentsMode.TONE
