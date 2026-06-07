"""Tests for the template selector.

Covers:
  - parse_library: accepts valid JSON, defaults the version, rejects bad shapes
  - parse_library: rejects duplicate ids
  - parse_library: empty string yields an empty library (not an error)
  - by_id only returns enabled templates
  - select_default_template: returns the chosen template
  - select_default_template: skips the API call when only one template is enabled
  - select_default_template: returns None on hallucinated id
  - select_default_template: returns None on selector failure
  - select_default_template: returns None on JSON parse failure
  - select_default_template: returns None when library is empty
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from bulkvid.adapters.openai_client import ChatResult, OpenAIClient
from bulkvid.pipeline.safety import SAFE
from bulkvid.pipeline.template_selector import (
    Template,
    TemplateLibrary,
    TemplateLibraryParseError,
    parse_library,
    select_default_template,
)


# ── parse_library ───────────────────────────────────────────────────────────


def test_parse_library_empty_string_returns_empty() -> None:
    lib = parse_library("")
    assert lib.version == 1
    assert lib.templates == ()


def test_parse_library_whitespace_returns_empty() -> None:
    assert parse_library("   \n  ").templates == ()


def test_parse_library_valid_payload_round_trips() -> None:
    payload = json.dumps(
        {
            "version": 1,
            "templates": [
                {
                    "id": "warm_opener",
                    "name": "Warm opener",
                    "hint": "Friendly podcast tone",
                    "body": "Warm conversational opener.",
                    "match_hints": {"vertical_any": ["lifestyle"]},
                },
                {
                    "id": "news_opener",
                    "name": "Urgent news opener",
                    "hint": "Punchy",
                    "body": "Lead with the most surprising fact.",
                    "enabled": False,
                },
            ],
        }
    )
    lib = parse_library(payload)
    assert lib.version == 1
    assert len(lib.templates) == 2
    assert lib.templates[0].id == "warm_opener"
    assert lib.templates[0].match_hints == {"vertical_any": ["lifestyle"]}
    assert lib.templates[1].enabled is False
    # Enabled view filters the disabled entry out.
    enabled = lib.enabled_templates()
    assert len(enabled) == 1
    assert enabled[0].id == "warm_opener"


def test_parse_library_rejects_bad_json() -> None:
    with pytest.raises(TemplateLibraryParseError):
        parse_library("{not json")


def test_parse_library_rejects_missing_id() -> None:
    payload = json.dumps({"templates": [{"name": "x", "body": "y"}]})
    with pytest.raises(TemplateLibraryParseError):
        parse_library(payload)


def test_parse_library_rejects_duplicate_ids() -> None:
    payload = json.dumps(
        {
            "templates": [
                {"id": "x", "name": "A", "body": "..."},
                {"id": "x", "name": "B", "body": "..."},
            ]
        }
    )
    with pytest.raises(TemplateLibraryParseError):
        parse_library(payload)


def test_parse_library_rejects_top_level_list() -> None:
    with pytest.raises(TemplateLibraryParseError):
        parse_library("[1, 2, 3]")


def test_by_id_returns_none_for_disabled() -> None:
    payload = json.dumps(
        {
            "templates": [
                {"id": "x", "name": "A", "body": "...", "enabled": False},
            ]
        }
    )
    lib = parse_library(payload)
    assert lib.by_id("x") is None


# ── select_default_template ─────────────────────────────────────────────────


def _two_template_library() -> TemplateLibrary:
    return TemplateLibrary(
        version=1,
        templates=(
            Template(id="warm", name="Warm", hint="friendly", body="warm body"),
            Template(id="urgent", name="Urgent", hint="punchy", body="urgent body"),
        ),
    )


def _make_fake_openai(chat_text: str) -> OpenAIClient:
    """Build an OpenAIClient with a mocked ``.chat`` method."""
    client = OpenAIClient(api_key="sk-test")
    mock_result = ChatResult(
        text=chat_text,
        prompt_tokens=100,
        completion_tokens=20,
        cost_usd=0.0001,
        model="gpt-5.4-mini",
    )
    client.chat = AsyncMock(return_value=mock_result)    # type: ignore[method-assign]
    return client


async def test_select_returns_chosen_template() -> None:
    lib = _two_template_library()
    client = _make_fake_openai(
        json.dumps({"template_id": "urgent", "reason": "article is news"})
    )

    chosen = await select_default_template(
        client,
        library=lib,
        vertical="news",
        country="US",
        article_title="Breaking: ...",
        article_excerpt="Something just happened.",
    )

    assert chosen is not None
    assert chosen.id == "urgent"
    assert chosen.body == "urgent body"
    client.chat.assert_awaited_once()    # type: ignore[attr-defined]


async def test_select_skips_call_when_only_one_enabled() -> None:
    """Single-template library bypasses the API entirely."""
    lib = TemplateLibrary(
        version=1,
        templates=(
            Template(id="solo", name="Solo", hint="x", body="b"),
        ),
    )
    client = _make_fake_openai("{}")    # would crash if actually called

    chosen = await select_default_template(
        client,
        library=lib,
        vertical="news",
        country="US",
        article_title="x",
        article_excerpt="y",
    )
    assert chosen is not None
    assert chosen.id == "solo"
    client.chat.assert_not_awaited()    # type: ignore[attr-defined]


async def test_select_returns_none_when_library_empty() -> None:
    client = _make_fake_openai("{}")
    chosen = await select_default_template(
        client,
        library=TemplateLibrary(version=1, templates=()),
        vertical="x", country="x", article_title="x", article_excerpt="x",
    )
    assert chosen is None
    client.chat.assert_not_awaited()    # type: ignore[attr-defined]


async def test_select_returns_none_on_hallucinated_id() -> None:
    lib = _two_template_library()
    client = _make_fake_openai(
        json.dumps({"template_id": "does_not_exist", "reason": "..."})
    )

    chosen = await select_default_template(
        client,
        library=lib,
        vertical="x", country="x", article_title="x", article_excerpt="x",
    )
    assert chosen is None


async def test_select_returns_none_on_disabled_id() -> None:
    """Returning the id of a disabled template is treated the same as
    hallucination — the operator wanted that template off."""
    # Need two enabled templates so the selector actually invokes OpenAI
    # (the single-enabled-template shortcut would otherwise bypass it).
    lib = TemplateLibrary(
        version=1,
        templates=(
            Template(id="enabled1", name="A", hint="x", body="a-body"),
            Template(id="enabled2", name="B", hint="y", body="b-body"),
            Template(id="off", name="C", hint="z", body="c-body", enabled=False),
        ),
    )
    client = _make_fake_openai(json.dumps({"template_id": "off", "reason": "..."}))

    chosen = await select_default_template(
        client,
        library=lib,
        vertical="x", country="x", article_title="x", article_excerpt="x",
    )
    assert chosen is None


async def test_select_returns_none_on_openai_exception() -> None:
    lib = _two_template_library()
    client = _make_fake_openai("ignored")
    client.chat = AsyncMock(side_effect=RuntimeError("openai blew up"))    # type: ignore[method-assign]

    chosen = await select_default_template(
        client,
        library=lib,
        vertical="x", country="x", article_title="x", article_excerpt="x",
    )
    assert chosen is None


async def test_select_returns_none_on_bad_json_response() -> None:
    lib = _two_template_library()
    client = _make_fake_openai("not actually json")

    chosen = await select_default_template(
        client,
        library=lib,
        vertical="x", country="x", article_title="x", article_excerpt="x",
    )
    assert chosen is None


async def test_select_uses_safety_block_when_matched() -> None:
    """When the safety context is flagged, the user message mentions it.
    Otherwise it must not."""
    from bulkvid.pipeline.safety import SafetyContext

    lib = _two_template_library()
    captured_messages: dict = {}

    async def _fake_chat(**kwargs):    # noqa: ANN003
        captured_messages.update(kwargs)
        return ChatResult(
            text=json.dumps({"template_id": "warm", "reason": "ok"}),
            prompt_tokens=10, completion_tokens=5, cost_usd=0.0,
            model="gpt-5.4-mini",
        )

    client = OpenAIClient(api_key="sk-test")
    client.chat = _fake_chat    # type: ignore[method-assign]

    safety_on = SafetyContext(matched=True, matched_keyword="lingerie")
    await select_default_template(
        client, library=lib,
        vertical="apparel", country="US",
        article_title="x", article_excerpt="y",
        safety=safety_on,
    )
    user_content = captured_messages["messages"][1]["content"]
    assert "SAFETY NOTE" in user_content

    captured_messages.clear()
    await select_default_template(
        client, library=lib,
        vertical="apparel", country="US",
        article_title="x", article_excerpt="y",
        safety=SAFE,
    )
    user_content = captured_messages["messages"][1]["content"]
    assert "SAFETY NOTE" not in user_content
