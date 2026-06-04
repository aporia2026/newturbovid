"""End-to-end prompt assembly for the sensitive-apparel safeguard.

For each tab (Simple / Simple x4 / Cartoon) we mock the OpenAI HTTP call,
generate a prompt against the real ``SettingsStore`` + the real prompt
builders, and assert the safety block is present iff the row's vertical
matches a keyword. Together with ``test_safety.py`` (pure-function unit
tests), this proves the safeguard fires end-to-end without invoking the
network or the row processor's heavier surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.orchestrator.runtime_settings import (
    SENSITIVE_APPAREL_RULES_DEFAULT,
    SETTING_CARTOON_PLANNER_PROMPT,
    SETTING_SIMPLE_SCRIPT_PROMPT,
    SETTING_SIMPLE_X4_SCRIPT_PROMPT,
    registry_defaults,
)
from bulkvid.orchestrator.settings_store import SettingsStore
from bulkvid.pipeline.cartoon_prompt import generate_cartoon_plan
from bulkvid.pipeline.image_prompt import build_collage_prompt
from bulkvid.pipeline.open_comments import OpenCommentsAnalysis, OpenCommentsMode
from bulkvid.pipeline.safety import SAFE, SafetyContext, resolve_safety
from bulkvid.pipeline.script_gen import generate_script

API_KEY = "sk-test"
BASE = "https://api.openai.com/v1"


# ── Shared mocks ─────────────────────────────────────────────────────────────


def _chat_response(content: str) -> dict:
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
            "prompt_tokens": 100,
            "completion_tokens": 30,
            "total_tokens": 130,
        },
    }


def _capture(captured: list[dict], response_content: str):
    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response(response_content))

    return _handler


def _system_text(captured: dict) -> str:
    return next(m["content"] for m in captured["messages"] if m["role"] == "system")


def _user_text(captured: dict) -> str:
    return next(m["content"] for m in captured["messages"] if m["role"] == "user")


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    s = SettingsStore(
        tmp_path / "settings.db",
        defaults=registry_defaults(),
        cache_ttl_seconds=0.0,
    )
    yield s
    s.close()


def _vanilla_analysis() -> OpenCommentsAnalysis:
    return OpenCommentsAnalysis(
        mode=OpenCommentsMode.NONE, raw_text="", tone_hints=[], directives=[]
    )


# A recognizable line from the safety block; if it appears in a prompt the
# safeguard is active for that call.
SAFETY_MARKER = "SENSITIVE APPAREL"


# ── resolve_safety reads from the store ──────────────────────────────────────


async def test_resolve_safety_matches_when_vertical_in_keywords(
    store: SettingsStore,
) -> None:
    safety = await resolve_safety(store, "Lingerie Boutique", row_num=1)
    assert safety.matched is True
    assert safety.matched_keyword == "lingerie"


async def test_resolve_safety_misses_for_non_sensitive_vertical(
    store: SettingsStore,
) -> None:
    safety = await resolve_safety(store, "Smart home gadgets", row_num=2)
    assert safety.matched is False
    assert safety.matched_keyword is None


async def test_resolve_safety_no_store_returns_safe() -> None:
    # When the store isn't wired (e.g. tests with no store), the helper
    # must NOT crash and must fall through to a safe-empty context.
    safety = await resolve_safety(None, "Lingerie shop")
    assert safety == SAFE


async def test_resolve_safety_uses_admin_keywords(
    store: SettingsStore,
) -> None:
    # Tighten the admin list to just one word; verify that other defaults
    # no longer match.
    from bulkvid.orchestrator.runtime_settings import (
        SETTING_SENSITIVE_APPAREL_KEYWORDS,
    )
    await store.set(SETTING_SENSITIVE_APPAREL_KEYWORDS, "swimwear", "test")
    assert (await resolve_safety(store, "Lingerie boutique")).matched is False
    assert (await resolve_safety(store, "Mens swimwear sale")).matched is True


# ── Simple / 4Images-VO2: script-gen prompt ──────────────────────────────────


@respx.mock
async def test_script_prompt_has_safety_block_when_sensitive(
    store: SettingsStore,
) -> None:
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(
        side_effect=_capture(captured, json.dumps({"script": "hi", "style_direction": "warm"}))
    )

    safety = await resolve_safety(store, "Lingerie boutique")
    async with OpenAIClient(api_key=API_KEY) as client:
        await generate_script(
            client,
            article_body="article",
            country="US",
            vertical="Lingerie boutique",
            language="en",
            script_pattern="How To",
            open_comments=_vanilla_analysis(),
            settings_store=store,
            prompt_setting_key=SETTING_SIMPLE_SCRIPT_PROMPT,
            safety=safety,
        )

    sys = _system_text(captured[0])
    assert SAFETY_MARKER in sys
    # The original prompt is still there (one of its compliance lines).
    assert "Hard maximum: 20 words" in sys


@respx.mock
async def test_script_prompt_has_no_safety_block_when_not_sensitive(
    store: SettingsStore,
) -> None:
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(
        side_effect=_capture(captured, json.dumps({"script": "hi", "style_direction": "warm"}))
    )

    safety = await resolve_safety(store, "Smart home gadgets")
    async with OpenAIClient(api_key=API_KEY) as client:
        await generate_script(
            client,
            article_body="article",
            country="US",
            vertical="Smart home gadgets",
            language="en",
            script_pattern="How To",
            open_comments=_vanilla_analysis(),
            settings_store=store,
            prompt_setting_key=SETTING_SIMPLE_SCRIPT_PROMPT,
            safety=safety,
        )

    sys = _system_text(captured[0])
    assert SAFETY_MARKER not in sys


@respx.mock
async def test_script_prompt_reads_simple_x4_template_independently(
    store: SettingsStore,
) -> None:
    """Editing the Simple x4 prompt must NOT affect the Simple prompt."""
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(
        side_effect=_capture(captured, json.dumps({"script": "hi", "style_direction": "warm"}))
    )

    await store.set(SETTING_SIMPLE_X4_SCRIPT_PROMPT, "X4-ONLY-MARKER {language}", "admin")

    async with OpenAIClient(api_key=API_KEY) as client:
        await generate_script(
            client,
            article_body="article",
            country="US",
            vertical="gadgets",
            language="en",
            script_pattern="How To",
            open_comments=_vanilla_analysis(),
            settings_store=store,
            prompt_setting_key=SETTING_SIMPLE_X4_SCRIPT_PROMPT,
        )
        await generate_script(
            client,
            article_body="article",
            country="US",
            vertical="gadgets",
            language="en",
            script_pattern="How To",
            open_comments=_vanilla_analysis(),
            settings_store=store,
            prompt_setting_key=SETTING_SIMPLE_SCRIPT_PROMPT,
        )

    sys_x4 = _system_text(captured[0])
    sys_simple = _system_text(captured[1])
    assert "X4-ONLY-MARKER" in sys_x4
    assert "X4-ONLY-MARKER" not in sys_simple


# ── Simple x4: collage image prompt ──────────────────────────────────────────


@respx.mock
async def test_collage_prompt_has_safety_block_when_sensitive(
    store: SettingsStore,
) -> None:
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(
        side_effect=_capture(captured, "A collage description")
    )

    safety = SafetyContext(matched=True, matched_keyword="lingerie")
    async with OpenAIClient(api_key=API_KEY) as client:
        await build_collage_prompt(
            client,
            description="An ad about lingerie.",
            article_excerpt="Lingerie launch.",
            settings_store=store,
            safety=safety,
        )

    user_msg = _user_text(captured[0])
    assert SAFETY_MARKER in user_msg
    # The structural collage instructions are still present.
    assert "STRICT 2x2 GRID" in user_msg


@respx.mock
async def test_collage_prompt_has_no_safety_block_when_safe(
    store: SettingsStore,
) -> None:
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(
        side_effect=_capture(captured, "A collage description")
    )

    async with OpenAIClient(api_key=API_KEY) as client:
        await build_collage_prompt(
            client,
            description="An ad about gadgets.",
            article_excerpt="Smart home.",
            settings_store=store,
            safety=SAFE,
        )

    user_msg = _user_text(captured[0])
    assert SAFETY_MARKER not in user_msg


# ── Cartoon: planner prompt ──────────────────────────────────────────────────


def _cartoon_response() -> str:
    return json.dumps(
        {
            "ideas": [
                {
                    "voiceover": "A short cartoon line about the product.",
                    "style_direction": "Warm.",
                    "shots": [
                        {"scene": "Product on a hanger.", "motion": "Slow zoom."},
                        {"scene": "Product flat-lay.", "motion": "Pan."},
                    ],
                },
                {
                    "voiceover": "Another short line about the product.",
                    "style_direction": "Warm.",
                    "shots": [
                        {"scene": "Product folded.", "motion": "Static."},
                        {"scene": "Product on a table.", "motion": "Static."},
                    ],
                },
            ]
        }
    )


@respx.mock
async def test_cartoon_planner_prompt_has_safety_block_when_sensitive(
    store: SettingsStore,
) -> None:
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(
        side_effect=_capture(captured, _cartoon_response())
    )

    safety = await resolve_safety(store, "Bra and panties shop")
    async with OpenAIClient(api_key=API_KEY) as client:
        await generate_cartoon_plan(
            client,
            article_body="article",
            country="US",
            vertical="Bra and panties shop",
            language="en",
            script_pattern="",
            open_comments=_vanilla_analysis(),
            settings_store=store,
            safety=safety,
        )

    sys = _system_text(captured[0])
    assert SAFETY_MARKER in sys
    assert "GENERIC, SYMBOLIC characters" in sys


@respx.mock
async def test_cartoon_planner_prompt_has_no_safety_block_when_safe(
    store: SettingsStore,
) -> None:
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(
        side_effect=_capture(captured, _cartoon_response())
    )

    async with OpenAIClient(api_key=API_KEY) as client:
        await generate_cartoon_plan(
            client,
            article_body="article",
            country="US",
            vertical="Smart home gadgets",
            language="en",
            script_pattern="",
            open_comments=_vanilla_analysis(),
            settings_store=store,
            safety=SAFE,
        )

    sys = _system_text(captured[0])
    assert SAFETY_MARKER not in sys


@respx.mock
async def test_cartoon_planner_uses_admin_edited_template(
    store: SettingsStore,
) -> None:
    await store.set(
        SETTING_CARTOON_PLANNER_PROMPT,
        "CARTOON-ADMIN-MARKER language={language} ideas={num_ideas} shots={num_shots}",
        "admin",
    )
    captured: list[dict] = []
    respx.post(f"{BASE}/chat/completions").mock(
        side_effect=_capture(captured, _cartoon_response())
    )

    async with OpenAIClient(api_key=API_KEY) as client:
        await generate_cartoon_plan(
            client,
            article_body="article",
            country="US",
            vertical="gadgets",
            language="he",
            script_pattern="",
            open_comments=_vanilla_analysis(),
            settings_store=store,
            safety=SAFE,
        )

    sys = _system_text(captured[0])
    assert "CARTOON-ADMIN-MARKER" in sys
    assert "language=he" in sys
    assert "ideas=2" in sys
    assert "shots=2" in sys
