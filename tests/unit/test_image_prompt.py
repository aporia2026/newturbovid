"""Tests for the image-prompt module.

Covers:
  - describe_source_image: vision call, returns text + cost
  - describe_source_image: empty response gets a sensible fallback
  - describe_source_image: reads on-image marketing text + CTA (prompt intent)
  - build_collage_prompt: returns text + cost, strips wrapping quotes
  - collage prompt now asks for SIMILAR marketing text + CTA per panel,
    forbids real brands/logos/trademarks, and no longer strips text
"""

from __future__ import annotations

import json

import httpx
import respx

from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.pipeline.image_prompt import (
    _DESCRIBE_PROMPT,
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


def test_describe_prompt_reads_marketing_text_and_cta() -> None:
    # The vision prompt must now capture the on-image text + CTA, not ignore it.
    lower = _DESCRIBE_PROMPT.lower()
    assert "marketing text" in lower
    assert "call-to-action" in lower or "cta" in lower
    assert "ignore all text" not in lower


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
        prompt, _ = await build_collage_prompt(client, description="A simple scene.")
    assert not prompt.startswith('"')
    assert not prompt.endswith('"')


@respx.mock
async def test_build_collage_asks_for_text_cta_and_forbids_real_brands() -> None:
    captured: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response("Create a 2x2 grid collage."))

    respx.post(f"{BASE}/chat/completions").mock(side_effect=_handler)
    async with OpenAIClient(api_key=API_KEY) as client:
        await build_collage_prompt(client, description="A street scene.")

    user_msg = captured[0]["messages"][1]["content"]
    # Layout still enforced.
    assert "2x2" in user_msg or "2 columns" in user_msg
    assert "TOP-LEFT" in user_msg
    assert "BOTTOM-RIGHT" in user_msg
    # Marketing text + CTA, in the ARTICLE's language (2026-06-08: switched
    # from "inspiration's language" to "article's language" when we made the
    # default look stop depending on the seed image — see the rewrite of
    # _collage_user_message).
    assert "headline" in user_msg.lower()
    assert "cta" in user_msg.lower() or "call-to-action" in user_msg.lower()
    assert "article's language" in user_msg.lower()
    # 3-band layout is mandatory (white top + photo + white bottom) so every
    # row produces the same shape regardless of seed style.
    assert "3-band" in user_msg.lower() or "three-band" in user_msg.lower()
    assert "solid white" in user_msg.lower() or "solid-white" in user_msg.lower()
    # The SAME headline / CTA on every cell (verbatim across the 4 panels).
    assert "verbatim" in user_msg.lower()
    # Real brands are forbidden in the generated panels (legal requirement).
    assert "no real brands" in user_msg.lower()
    assert "trademark" in user_msg.lower()
    # Regression guard: the prompt must NOT tell kie to keep the inspiration's
    # layout — that produced the seed-dependent variation Yoav complained
    # about. The new prompt mandates our own 3-band shape regardless of seed.
    assert "AUTOMOTIVE RULE" not in user_msg


@respx.mock
async def test_build_collage_grounds_new_photo_in_article() -> None:
    captured: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response("Create a 2x2 grid collage."))

    respx.post(f"{BASE}/chat/completions").mock(side_effect=_handler)
    async with OpenAIClient(api_key=API_KEY) as client:
        await build_collage_prompt(
            client,
            description="A street scene.",
            article_excerpt="Modern prefab granny pods for backyards in 2026.",
        )

    user_msg = captured[0]["messages"][1]["content"]
    assert "ARTICLE CONTEXT" in user_msg
    assert "granny pods" in user_msg.lower()
