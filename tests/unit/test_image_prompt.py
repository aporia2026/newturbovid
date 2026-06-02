"""Tests for the image-prompt module.

Covers:
  - describe_source_image: vision call, returns text + cost
  - describe_source_image: empty response gets a sensible fallback
  - build_collage_prompt: returns text + cost, strips wrapping quotes
  - automotive rule auto-injects when vehicles are present in description
  - automotive rule absent otherwise (no false positives on "carrot" etc)
  - All required layout / brand / content rules end up in the user message
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.pipeline.image_prompt import (
    _automotive_rule_for,
    build_collage_prompt,
    describe_source_image,
)

API_KEY = "sk-test"
BASE = "https://api.openai.com/v1"


def _chat_response(content: str, ptokens: int = 200, ctokens: int = 80) -> dict:
    return {
        "id": "x",
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


# ── describe_source_image ───────────────────────────────────────────────────


@respx.mock
async def test_describe_returns_text_and_cost() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_chat_response(
                "SUBJECT: A red sports car. SETTING: A coastal highway."
            ),
        )
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        desc, cost = await describe_source_image(client, image_b64="ABC=")
    assert "sports car" in desc
    assert cost > 0


@respx.mock
async def test_describe_empty_response_gets_fallback() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_response(""))
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        desc, _ = await describe_source_image(client, image_b64="ABC=")
    assert desc != ""
    assert "advertising" in desc.lower() or "photograph" in desc.lower()


@respx.mock
async def test_describe_sends_image_in_user_content() -> None:
    captured: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response("A scene."))

    respx.post(f"{BASE}/chat/completions").mock(side_effect=_handler)
    async with OpenAIClient(api_key=API_KEY) as client:
        await describe_source_image(client, image_b64="ZZZ=")

    user_content = captured[0]["messages"][0]["content"]
    assert isinstance(user_content, list)
    # text + image_url parts.
    types = [p["type"] for p in user_content]
    assert "text" in types
    assert "image_url" in types
    image_part = next(p for p in user_content if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,ZZZ")


# ── build_collage_prompt ────────────────────────────────────────────────────


@respx.mock
async def test_build_collage_returns_text_and_cost() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_chat_response("Create a 2x2 grid collage. TOP-LEFT: scene one.")
        )
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        prompt, cost = await build_collage_prompt(
            client, description="A red sports car on a coastal road."
        )
    assert "2x2" in prompt or "grid" in prompt.lower()
    assert cost > 0


@respx.mock
async def test_build_collage_strips_wrapping_quotes() -> None:
    quoted_response = '"Create a 2x2 grid collage. TOP-LEFT: scene one."'
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_response(quoted_response))
    )
    async with OpenAIClient(api_key=API_KEY) as client:
        prompt, _ = await build_collage_prompt(
            client, description="A simple scene."
        )
    assert not prompt.startswith('"')
    assert not prompt.endswith('"')


@respx.mock
async def test_build_collage_includes_layout_brand_content_rules() -> None:
    captured: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response("Create a 2x2 grid collage."))

    respx.post(f"{BASE}/chat/completions").mock(side_effect=_handler)
    async with OpenAIClient(api_key=API_KEY) as client:
        await build_collage_prompt(client, description="A street scene.")

    user_msg = captured[0]["messages"][1]["content"]
    # Layout
    assert "2x2" in user_msg or "2-column" in user_msg
    assert "TOP-LEFT" in user_msg
    assert "BOTTOM-RIGHT" in user_msg
    # Brand
    assert "NO car brand logos" in user_msg or "NO brand names" in user_msg
    # Content (NO text inside panels)
    assert "NO text" in user_msg
    assert "NO logos" in user_msg


# ── Automotive rule ─────────────────────────────────────────────────────────


def test_automotive_rule_fires_on_vehicle_descriptions() -> None:
    rule = _automotive_rule_for("A red sports car parked on a coastal road")
    assert rule != ""
    assert "aerial" in rule.lower() or "overhead" in rule.lower()


def test_automotive_rule_silent_on_non_vehicle_descriptions() -> None:
    rule = _automotive_rule_for("A bouquet of red flowers on a wooden table")
    assert rule == ""


def test_automotive_rule_catches_brand_names() -> None:
    rule = _automotive_rule_for("A BMW sedan in a parking lot")
    assert rule != ""


@respx.mock
async def test_build_collage_injects_automotive_rule_when_relevant() -> None:
    captured: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response("Create..."))

    respx.post(f"{BASE}/chat/completions").mock(side_effect=_handler)
    async with OpenAIClient(api_key=API_KEY) as client:
        await build_collage_prompt(
            client, description="A Toyota sedan parked outside a dealership."
        )
    user_msg = captured[0]["messages"][1]["content"]
    assert "AUTOMOTIVE RULE" in user_msg
    assert "aerial" in user_msg.lower() or "bird's-eye" in user_msg.lower()


@respx.mock
async def test_build_collage_skips_automotive_rule_when_irrelevant() -> None:
    captured: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response("Create..."))

    respx.post(f"{BASE}/chat/completions").mock(side_effect=_handler)
    async with OpenAIClient(api_key=API_KEY) as client:
        await build_collage_prompt(client, description="A bowl of fresh fruit.")
    user_msg = captured[0]["messages"][1]["content"]
    assert "AUTOMOTIVE RULE" not in user_msg
