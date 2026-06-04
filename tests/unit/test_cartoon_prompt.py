"""Tests for the cartoon-mode planner.

OpenAI is mocked via respx. Covers:
  - Well-formed plan -> exactly num_ideas ideas of num_shots shots each
  - Malformed JSON -> generic fallback fills to num_ideas (row still ships)
  - Short plan (too few ideas) -> padded with fallback ideas
  - Voiceover word-cap backstop (deterministic, so VO never outruns the video)
  - Permissive _coerce_ideas: alt key names, bare-string shots, shot padding
  - Diagnostic raw_preview log when the planner returns an incomplete plan
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
    complete_voiceover,
    generate_cartoon_plan,
    image_prompt_for_shot,
    shorten_voiceover,
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
async def test_plan_accepts_alt_voiceover_key() -> None:
    # Live run 2026-06-03 #5 had _coerce_ideas reject all ideas, dropping the
    # row to the generic fallback. Permissive shape accepts common alt key
    # names so transient model drift doesn't trigger the cliff.
    alt_shape = json.dumps(
        {
            "ideas": [
                {
                    "voice_over": "A short clean line about cars this spring.",
                    "tone": "Warm.",
                    "scenes": [
                        {"description": "A person walks past a parked compact car.",
                         "action": "slow pan left to right"},
                        {"description": "Close-up of hands at a steering wheel.",
                         "action": "subtle camera push-in"},
                    ],
                },
                {
                    "vo": "Buyers are checking prices before the weekend.",
                    "delivery": "Calm.",
                    "sequence": [
                        {"visual": "A laptop on a kitchen table.", "movement": "static"},
                        {"prompt": "A coffee mug beside the laptop.", "animation": "slow zoom"},
                    ],
                },
            ]
        }
    )
    plan = await _run(alt_shape)
    assert len(plan.ideas) == 2
    # Both ideas use alt keys but still produce real content (not fallback text).
    assert "compact car" in plan.ideas[0].shots[0].scene
    assert "kitchen table" in plan.ideas[1].shots[0].scene
    # No idea ended up as the fallback ("Here's what you should know about ...").
    assert all("you should know" not in idea.voiceover for idea in plan.ideas)


@respx.mock
async def test_plan_accepts_bare_string_shots() -> None:
    # Some model outputs list scenes as bare strings instead of {scene, motion}.
    # The permissive coercer should treat each string as a scene description
    # and supply a default motion.
    shape = json.dumps(
        {
            "ideas": [
                {
                    "voiceover": "A short line about cars.",
                    "style_direction": "Warm.",
                    "shots": [
                        "A person walks past a compact car.",
                        "Close-up of hands at a steering wheel.",
                    ],
                },
                {
                    "voiceover": "Another short line about cars.",
                    "style_direction": "Calm.",
                    "shots": [
                        "A laptop on a kitchen table.",
                        "A coffee mug beside the laptop.",
                    ],
                },
            ]
        }
    )
    plan = await _run(shape)
    assert len(plan.ideas) == 2
    assert plan.ideas[0].shots[0].scene == "A person walks past a compact car."
    assert plan.ideas[0].shots[0].motion           # default motion supplied
    assert all("you should know" not in idea.voiceover for idea in plan.ideas)


@respx.mock
async def test_plan_pads_short_shot_list_by_repeating_last() -> None:
    # Model returned only 1 shot when 2 were requested. Old behavior: reject
    # the whole idea -> fallback. New behavior: pad with a copy of the last
    # valid shot so the idea ships with real content. (image-to-image chaining
    # downstream keeps the visual cohesive.)
    shape = json.dumps(
        {
            "ideas": [
                {
                    "voiceover": "Cars are cheaper this spring.",
                    "style_direction": "Warm.",
                    "shots": [
                        {"scene": "A person walks past a compact car.",
                         "motion": "slow pan"},
                    ],
                },
                {
                    "voiceover": "Buyers are checking prices.",
                    "style_direction": "Calm.",
                    "shots": [
                        {"scene": "A laptop on a kitchen table.", "motion": "static"},
                    ],
                },
            ]
        }
    )
    plan = await _run(shape, num_ideas=2, num_shots=2)
    assert len(plan.ideas) == 2
    for idea in plan.ideas:
        assert len(idea.shots) == 2
        # Padded shot reuses the same scene as the previous (last-good) shot.
        assert idea.shots[0].scene == idea.shots[1].scene


@respx.mock
async def test_plan_voiceover_above_cap_drops_idea_then_falls_back() -> None:
    # Model returns voiceovers well above CARTOON_MAX_WORDS where no sentence
    # boundary exists inside the cap. Old behaviour: truncate to N words +
    # append a synthetic ".", producing fragments like "...inventory than they."
    # New behaviour: _enforce_word_cap returns "" and _coerce_ideas drops the
    # idea, the planner pads to num_ideas with the generic fallback. The row
    # still ships, but with a fallback line rather than a chopped fragment.
    long_vo = (
        "Used car prices are dropping fast this spring as buyers grow more "
        "cautious and dealers face tighter inventory than they expected."
    )  # 22 words, single sentence — no usable boundary inside the 12-word cap
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
        # The shipped voiceovers are all complete sentences within the cap.
        assert len(idea.voiceover.split()) <= CARTOON_MAX_WORDS
        assert idea.voiceover.rstrip().endswith((".", "!", "?"))
        # And specifically NOT a fragment of the long source — the offending
        # ideas were dropped, not patched up with a fake period.
        assert "inventory than they" not in idea.voiceover


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


def test_enforce_word_cap_no_sentence_boundary_returns_empty() -> None:
    # A run-on with no usable sentence break used to be silently chopped at the
    # word boundary + an artificial period appended, which produced fragments
    # like "...with." that sounded cut. The helper now returns "" instead, so
    # the caller (_coerce_ideas) drops the idea rather than shipping a fragment.
    text = "this is a long run on line with no punctuation anywhere at all here"
    out = _enforce_word_cap(text, max_words=6)
    assert out == ""


def test_enforce_word_cap_comma_fragment_returns_empty() -> None:
    # A comma-separated run without any real sentence ending also gets dropped:
    # appending a period to "...third part." produced a grammatically-valid but
    # semantically incomplete VO. Returning "" lets the caller drop the idea.
    text = "first part, second part, third part, fourth part, fifth part"
    out = _enforce_word_cap(text, max_words=4)
    assert out == ""


# ── Sentence-boundary validation in _coerce_ideas ───────────────────────────


@respx.mock
async def test_plan_drops_idea_ending_on_conjunction() -> None:
    """Planner-returned VO that ends mid-thought (e.g. on 'and') used to ship
    via TTS and produce a video whose audio sounds cut. Now the idea is
    rejected outright and the row falls back to padding."""
    fragment = "Homes keep grandparents close to families and"   # ends on conjunction, no .!?
    plan_json = json.dumps(
        {
            "ideas": [
                {
                    "voiceover": fragment,
                    "style_direction": "Warm.",
                    "shots": [
                        {"scene": "Scene A", "motion": "subtle"},
                        {"scene": "Scene B", "motion": "subtle"},
                    ],
                },
                {
                    "voiceover": "A clean complete sentence about cheap cars.",
                    "style_direction": "Upbeat.",
                    "shots": [
                        {"scene": "Scene C", "motion": "subtle"},
                        {"scene": "Scene D", "motion": "subtle"},
                    ],
                },
            ]
        }
    )
    plan = await _run(plan_json)
    assert len(plan.ideas) == 2
    # The clean idea survives; the fragment is dropped + replaced by fallback.
    assert any("cheap cars" in idea.voiceover for idea in plan.ideas)
    # No idea ships the fragment.
    assert all(fragment not in idea.voiceover for idea in plan.ideas)
    # Every shipped idea ends cleanly.
    for idea in plan.ideas:
        assert idea.voiceover.rstrip().endswith((".", "!", "?"))


@respx.mock
async def test_plan_drops_idea_with_no_terminal_punctuation() -> None:
    """Any planner VO that lacks .!? at the end is dropped, regardless of how
    'complete' it might sound semantically. Defensive enforcement of the
    planner-prompt contract."""
    plan_json = json.dumps(
        {
            "ideas": [
                {
                    "voiceover": "Cars are cheaper this spring",   # no terminal punctuation
                    "style_direction": "Calm.",
                    "shots": [
                        {"scene": "Scene A", "motion": "subtle"},
                        {"scene": "Scene B", "motion": "subtle"},
                    ],
                },
                {
                    "voiceover": "A short clean sentence about cars.",
                    "style_direction": "Calm.",
                    "shots": [
                        {"scene": "Scene C", "motion": "subtle"},
                        {"scene": "Scene D", "motion": "subtle"},
                    ],
                },
            ]
        }
    )
    plan = await _run(plan_json)
    assert len(plan.ideas) == 2
    # The unpunctuated idea is dropped; the clean idea survives.
    assert any("short clean sentence" in idea.voiceover for idea in plan.ideas)
    assert all("Cars are cheaper this spring" not in idea.voiceover for idea in plan.ideas)


def test_cartoon_word_constants_consistent() -> None:
    # Guard the contract: min ≤ target ≤ max, all positive. The row processor
    # now hard-caps the video at 8.0s and shortens-and-retries any VO that
    # measures > MAX_EFFECTIVE_VO_SECONDS, so the word ceiling must be tight
    # enough to keep slow TTS deliveries inside that window most of the time.
    assert 0 < CARTOON_MIN_WORDS <= CARTOON_TARGET_WORDS <= CARTOON_MAX_WORDS
    assert CARTOON_MAX_WORDS <= 13


# ── complete_voiceover ──────────────────────────────────────────────────────


@respx.mock
async def test_complete_voiceover_happy_path() -> None:
    """Model is given a fragment, returns a complete sentence; helper accepts."""
    respx.post(f"{OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_chat_resp(
                json.dumps({"voiceover": "Homes keep grandparents close."})
            ),
        )
    )
    client = OpenAIClient(api_key="sk-test")
    fragment = "Homes keep grandparents close to families and"   # ends on conjunction
    out = await complete_voiceover(
        client, text=fragment, language="en", target_words=CARTOON_TARGET_WORDS,
    )
    assert out.voiceover == "Homes keep grandparents close."
    assert out.voiceover.rstrip().endswith(".")
    assert out.cost_usd > 0


@respx.mock
async def test_complete_voiceover_falls_back_on_bad_json() -> None:
    """A non-JSON model response returns the original text — caller decides to drop."""
    respx.post(f"{OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_resp("not json"))
    )
    client = OpenAIClient(api_key="sk-test")
    fragment = "Homes keep grandparents close to families and"
    out = await complete_voiceover(
        client, text=fragment, language="en", target_words=CARTOON_TARGET_WORDS,
    )
    assert out.voiceover == fragment    # unchanged on parse failure


@respx.mock
async def test_complete_voiceover_rejects_still_fragment_rewrite() -> None:
    """If the model returns text that still doesn't end on .!?, we return the
    original text (same signal as a parse failure)."""
    respx.post(f"{OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_chat_resp(
                json.dumps({"voiceover": "Homes keep grandparents close with"})
            ),
        )
    )
    client = OpenAIClient(api_key="sk-test")
    fragment = "Homes keep grandparents close to families and"
    out = await complete_voiceover(
        client, text=fragment, language="en", target_words=CARTOON_TARGET_WORDS,
    )
    assert out.voiceover == fragment    # still a fragment -> drop signal


@respx.mock
async def test_complete_voiceover_empty_returns_original() -> None:
    respx.post(f"{OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_chat_resp(json.dumps({"voiceover": ""})),
        )
    )
    client = OpenAIClient(api_key="sk-test")
    fragment = "Homes keep grandparents close to families and"
    out = await complete_voiceover(
        client, text=fragment, language="en", target_words=CARTOON_TARGET_WORDS,
    )
    assert out.voiceover == fragment


# ── Fragment recovery wired into generate_cartoon_plan ──────────────────────


@respx.mock
async def test_plan_recovers_fragment_via_complete_voiceover() -> None:
    """End-to-end recovery path: planner returns one fragment + one valid;
    the recovery step rewrites the fragment as a complete sentence; both
    ideas ship without falling back to padding."""
    planner_response = _chat_resp(
        json.dumps(
            {
                "ideas": [
                    {
                        # Fragment — ends on conjunction
                        "voiceover": "Cars cheaper this spring with",
                        "style_direction": "Upbeat.",
                        "shots": [
                            {"scene": "Scene A", "motion": "subtle"},
                            {"scene": "Scene B", "motion": "subtle"},
                        ],
                    },
                    {
                        # Clean
                        "voiceover": "Buyers find better deals before summer.",
                        "style_direction": "Calm.",
                        "shots": [
                            {"scene": "Scene C", "motion": "subtle"},
                            {"scene": "Scene D", "motion": "subtle"},
                        ],
                    },
                ]
            }
        )
    )
    recovery_response = _chat_resp(
        json.dumps({"voiceover": "Cars are cheaper this spring."})
    )
    respx.post(f"{OPENAI_BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json=planner_response),
            httpx.Response(200, json=recovery_response),
        ]
    )
    client = OpenAIClient(api_key="sk-test")
    plan = await generate_cartoon_plan(
        client,
        article_body="Used car prices are dropping.",
        country="US", vertical="automotive", language="en",
        script_pattern="How To", open_comments=_analysis(),
        num_ideas=2, num_shots=2,
    )
    assert len(plan.ideas) == 2
    voiceovers = [idea.voiceover for idea in plan.ideas]
    # The recovered fragment is in the result; no fallback "Here's what you
    # should know about..." was needed.
    assert any("Cars are cheaper this spring." in vo for vo in voiceovers)
    assert any("better deals before summer" in vo for vo in voiceovers)
    assert all("you should know" not in vo for vo in voiceovers)
    # Every shipped VO ends cleanly.
    assert all(vo.rstrip().endswith((".", "!", "?")) for vo in voiceovers)


@respx.mock
async def test_plan_unrecoverable_fragment_falls_back_to_padding() -> None:
    """If recovery itself produces another fragment, the idea is left dropped
    and the existing fallback padding fills the slot — exactly the pre-recovery
    behaviour, just with one extra (cheap) LLM call attempted."""
    planner_response = _chat_resp(
        json.dumps(
            {
                "ideas": [
                    {
                        "voiceover": "Cars cheaper this spring with",
                        "style_direction": "Upbeat.",
                        "shots": [
                            {"scene": "Scene A", "motion": "subtle"},
                            {"scene": "Scene B", "motion": "subtle"},
                        ],
                    },
                    {
                        "voiceover": "Buyers find better deals before summer.",
                        "style_direction": "Calm.",
                        "shots": [
                            {"scene": "Scene C", "motion": "subtle"},
                            {"scene": "Scene D", "motion": "subtle"},
                        ],
                    },
                ]
            }
        )
    )
    # Recovery LLM ALSO returns a fragment -> complete_voiceover returns the
    # original, which signals "still a fragment" to the planner.
    recovery_response = _chat_resp(
        json.dumps({"voiceover": "Cars cheaper this spring with"})
    )
    respx.post(f"{OPENAI_BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json=planner_response),
            httpx.Response(200, json=recovery_response),
        ]
    )
    client = OpenAIClient(api_key="sk-test")
    plan = await generate_cartoon_plan(
        client,
        article_body="Used car prices are dropping.",
        country="US", vertical="automotive", language="en",
        script_pattern="How To", open_comments=_analysis(),
        num_ideas=2, num_shots=2,
    )
    assert len(plan.ideas) == 2
    voiceovers = [idea.voiceover for idea in plan.ideas]
    # Clean idea survives; fragment is replaced by the generic fallback
    # ("Here's what you should know about ..." pattern).
    assert any("better deals before summer" in vo for vo in voiceovers)
    assert any("you should know" in vo for vo in voiceovers)
    # And no fragment slips through to the shipped result.
    assert all("Cars cheaper this spring with" not in vo for vo in voiceovers)


# ── shorten_voiceover ───────────────────────────────────────────────────────


@respx.mock
async def test_shorten_voiceover_returns_shorter_text() -> None:
    """Happy path: the shortener returns a JSON object with a shorter VO; the
    helper trims to ``target_words`` and reports it back."""
    respx.post(f"{OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_chat_resp(json.dumps({"voiceover": "Prices dropped this spring."})),
        )
    )
    client = OpenAIClient(api_key="sk-test")
    original = (
        "Used car prices have been falling fast across major US cities "
        "this spring as buyers grow more cautious about big purchases."
    )  # 20+ words
    out = await shorten_voiceover(
        client, text=original, language="en", target_words=6
    )
    assert out.voiceover == "Prices dropped this spring."
    assert len(out.voiceover.split()) <= 6
    assert out.cost_usd > 0


@respx.mock
async def test_shorten_voiceover_falls_back_on_bad_json() -> None:
    """A non-JSON model response must not crash; helper returns the original
    text so the caller can decide (typically: drop the idea)."""
    respx.post(f"{OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_resp("not json at all"))
    )
    client = OpenAIClient(api_key="sk-test")
    original = "A long sentence that the model failed to shorten cleanly."
    out = await shorten_voiceover(
        client, text=original, language="en", target_words=5
    )
    assert out.voiceover == original   # unchanged on parse failure


@respx.mock
async def test_shorten_voiceover_never_lengthens() -> None:
    """If the 'shorter' rewrite is actually the same length or longer than the
    original, the helper rejects it and returns the original. Protects against
    the caller deciding 'shorten worked' when the model just paraphrased."""
    respx.post(f"{OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_chat_resp(
                json.dumps(
                    {
                        "voiceover": (
                            "This rewritten line is intentionally exactly as "
                            "long as the input to verify the backstop."
                        )
                    }
                )
            ),
        )
    )
    client = OpenAIClient(api_key="sk-test")
    original = "A shorter line about the topic this morning."   # 8 words
    out = await shorten_voiceover(
        client, text=original, language="en", target_words=4
    )
    assert out.voiceover == original
    assert len(out.voiceover.split()) <= len(original.split())


@respx.mock
async def test_shorten_voiceover_empty_returns_original() -> None:
    """Empty 'voiceover' field in the response → fall back to the original."""
    respx.post(f"{OPENAI_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_chat_resp(json.dumps({"voiceover": "   "}))
        )
    )
    client = OpenAIClient(api_key="sk-test")
    original = "Cars are cheaper this spring than last spring."
    out = await shorten_voiceover(
        client, text=original, language="en", target_words=5
    )
    assert out.voiceover == original
