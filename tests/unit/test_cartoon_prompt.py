"""Tests for the cartoon-mode planner.

OpenAI is mocked via respx. Covers:
  - Well-formed plan -> exactly num_ideas ideas of num_shots shots each
  - Malformed JSON -> generic fallback fills to num_ideas (row still ships)
  - Short plan (too few ideas) -> padded with fallback ideas
  - Voiceover word-cap backstop (deterministic, so VO never outruns the video)
  - image_prompt_for_shot composition (style preamble; consistency clause on chained)
"""

from __future__ import annotations

import json

import httpx
import respx

from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.pipeline.cartoon_prompt import (
    CARTOON_MAX_WORDS,
    CARTOON_MIN_WORDS,
    CARTOON_STYLE,
    CARTOON_TARGET_WORDS,
    CONSISTENCY_CLAUSE,
    NO_BRANDING,
    _enforce_word_cap,
    generate_cartoon_plan,
    image_prompt_for_shot,
)
from bulkvid.pipeline.open_comments import OpenCommentsAnalysis, OpenCommentsMode

OPENAI_BASE = "https://api.openai.com/v1"


def _chat_resp(content: str) -> dict:
    return {
        "id": "x", "object": "chat.completion", "created": 1717_000_000,
        "model": "gpt-5.4-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 120, "total_tokens": 320},
    }


def _analysis() -> OpenCommentsAnalysis:
    return OpenCommentsAnalysis(raw_text="", mode=OpenCommentsMode.NONE)


def _good_plan_json(num_ideas: int = 2, num_shots: int = 2) -> str:
    return json.dumps(
        {
            "ideas": [
                {
                    "voiceover": f"Idea {i+1}: cars are cheaper this spring.",
                    "style_direction": "Upbeat and warm.",
                    "shots": [
                        {"scene": f"A cartoon person, scene {i+1}.{s+1}.",
                         "motion": "gentle camera push-in"}
                        for s in range(num_shots)
                    ],
                }
                for i in range(num_ideas)
            ]
        }
    )


async def _run(content: str, *, num_ideas: int = 2, num_shots: int = 2):
    respx.post(f"{OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_resp(content))
    )
    client = OpenAIClient(api_key="sk-test")
    return await generate_cartoon_plan(
        client,
        article_body="Used car prices are dropping.",
        country="US", vertical="automotive", language="en",
        script_pattern="How To", open_comments=_analysis(),
        num_ideas=num_ideas, num_shots=num_shots,
    )


@respx.mock
async def test_plan_well_formed() -> None:
    plan = await _run(_good_plan_json(2, 2))
    assert len(plan.ideas) == 2
    assert all(len(idea.shots) == 2 for idea in plan.ideas)
    assert all(idea.voiceover for idea in plan.ideas)
    assert plan.cost_usd > 0


@respx.mock
async def test_plan_malformed_json_falls_back() -> None:
    plan = await _run("this is not json at all")
    # Fallback still produces the requested shape so the row ships.
    assert len(plan.ideas) == 2
    assert all(len(idea.shots) == 2 for idea in plan.ideas)
    assert all(idea.voiceover for idea in plan.ideas)


@respx.mock
async def test_plan_too_few_ideas_padded() -> None:
    one_idea = json.dumps(
        {
            "ideas": [
                {
                    "voiceover": "Only one idea returned.",
                    "style_direction": "Calm.",
                    "shots": [
                        {"scene": "Scene A", "motion": "subtle"},
                        {"scene": "Scene B", "motion": "subtle"},
                    ],
                }
            ]
        }
    )
    plan = await _run(one_idea)
    assert len(plan.ideas) == 2    # padded to num_ideas


@respx.mock
async def test_plan_drops_shot_short_ideas_then_pads() -> None:
    # An idea with the wrong shot count is rejected by _coerce_ideas, then padded.
    bad_shape = json.dumps(
        {"ideas": [{"voiceover": "x", "style_direction": "y", "shots": [{"scene": "only one"}]}]}
    )
    plan = await _run(bad_shape, num_ideas=2, num_shots=2)
    assert len(plan.ideas) == 2
    assert all(len(idea.shots) == 2 for idea in plan.ideas)


@respx.mock
async def test_plan_voiceover_above_cap_is_truncated() -> None:
    # Model returns voiceovers well above CARTOON_MAX_WORDS. The cap must kick
    # in deterministically (so the TTS step never produces a clip longer than
    # the assembled video can play). Regression for the 2026-06-03 live overshoot.
    long_vo = (
        "Used car prices are dropping fast this spring as buyers grow more "
        "cautious and dealers face tighter inventory than they expected."
    )  # 22 words
    plan_json = json.dumps(
        {
            "ideas": [
                {
                    "voiceover": long_vo,
                    "style_direction": "Calm.",
                    "shots": [
                        {"scene": "Scene A", "motion": "subtle"},
                        {"scene": "Scene B", "motion": "subtle"},
                    ],
                }
            ] * 2
        }
    )
    plan = await _run(plan_json)
    assert len(plan.ideas) == 2
    for idea in plan.ideas:
        assert len(idea.voiceover.split()) <= CARTOON_MAX_WORDS
        # Truncated line still ends cleanly (no mid-word cut, no dangling comma).
        assert idea.voiceover.rstrip().endswith((".", "!", "?"))


def test_image_prompt_for_shot_first_shot() -> None:
    p = image_prompt_for_shot("A person in a car.", is_chained=False)
    assert p.startswith(CARTOON_STYLE)
    assert "A person in a car." in p
    assert NO_BRANDING in p             # brand-safety clause always present
    assert CONSISTENCY_CLAUSE not in p


def test_image_prompt_for_shot_chained_shot() -> None:
    p = image_prompt_for_shot("The same person waving.", is_chained=True)
    assert CARTOON_STYLE in p
    assert NO_BRANDING in p             # brand-safety clause always present
    assert CONSISTENCY_CLAUSE in p


def test_no_branding_clause_forbids_logos_and_plates() -> None:
    # Guard the wording so the brand-safety intent can't silently drift.
    lowered = NO_BRANDING.lower()
    assert "logo" in lowered
    assert "brand" in lowered
    assert "license-plate" in lowered or "license plate" in lowered


def test_enforce_word_cap_under_limit_preserved() -> None:
    # Already within the cap — text comes back unchanged (modulo trim).
    text = "A short clean line well under the cap."
    assert _enforce_word_cap(text, max_words=CARTOON_MAX_WORDS) == text


def test_enforce_word_cap_over_limit_prefers_sentence_boundary() -> None:
    # Two sentences; the cap should land at the first sentence's period rather
    # than mid-thought through the second.
    text = (
        "Used car prices are dropping fast this spring. "
        "Buyers should check inventory before the holiday weekend rush hits."
    )
    out = _enforce_word_cap(text, max_words=11)
    assert out.endswith(".")
    assert len(out.split()) <= 11
    assert "Used car prices are dropping fast this spring." in out
    assert "holiday" not in out          # tail of sentence 2 did not bleed in


def test_enforce_word_cap_no_sentence_boundary_falls_back_to_word_cut() -> None:
    # A run-on with no usable sentence break — the helper trims at the word
    # boundary and adds a terminal period so TTS reads naturally.
    text = "this is a long run on line with no punctuation anywhere at all here"
    out = _enforce_word_cap(text, max_words=6)
    assert out.endswith(".")
    assert len(out.rstrip(".").split()) == 6


def test_enforce_word_cap_strips_trailing_punctuation_before_period() -> None:
    # A truncation that lands on a comma-ended fragment must lose the comma
    # before the synthetic period is added (no ", ." artifacts).
    text = "first part, second part, third part, fourth part, fifth part"
    out = _enforce_word_cap(text, max_words=4)
    assert out.endswith(".")
    assert ", ." not in out
    assert ",." not in out


def test_cartoon_word_constants_consistent() -> None:
    # Guard the contract: min ≤ target ≤ max, all positive. The row processor's
    # [4, 8]s output clamp + audio fade backstops the upper bound, so the word
    # max just needs to keep TTS bills sane and avoid mid-thought truncations.
    assert 0 < CARTOON_MIN_WORDS <= CARTOON_TARGET_WORDS <= CARTOON_MAX_WORDS
    assert CARTOON_MAX_WORDS <= 16
