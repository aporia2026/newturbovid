"""Tests for the cartoon-mode planner.

OpenAI is mocked via respx. Covers:
  - Well-formed plan -> exactly num_ideas ideas of num_shots shots each
  - Malformed JSON -> generic fallback fills to num_ideas (row still ships)
  - Short plan (too few ideas) -> padded with fallback ideas
  - image_prompt_for_shot composition (style preamble; consistency clause on chained)
"""

from __future__ import annotations

import json

import httpx
import respx

from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.pipeline.cartoon_prompt import (
    CARTOON_STYLE,
    CONSISTENCY_CLAUSE,
    NO_BRANDING,
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
