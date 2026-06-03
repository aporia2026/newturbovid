"""Cartoon-mode planner — turns an article into animated video ideas.

One gpt-5.4-mini call (JSON mode) produces ``num_ideas`` independent video ideas
from the article. Each idea is a tiny multi-shot story:

  - ``voiceover``        — one short line (~6-7s spoken) in the detected language
  - ``style_direction``  — delivery hint for the Gemini TTS step
  - ``shots``            — ``num_shots`` scenes, each a SCENE description (for the
                           nano-banana-2 image step) plus a MOTION description
                           (for the Seedance image-to-video step)

Hard rules baked into the system prompt (plan §"Security & safety"):
  - GENERIC / SYMBOLIC characters only — never a real, named person's likeness.
  - One recurring character per idea, described consistently across its shots so
    the image-to-image chaining holds the look across the cut.
  - NO legible on-screen text (screens, signs, captions) — keep them abstract.

The shared cartoon STYLE preamble is composed onto each scene by the row
processor (``image_prompt_for_shot``), not here, so the planner stays focused on
content.

Model: gpt-5.4-mini (Yoav directive, plan §5 "Models locked in").
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from bulkvid.adapters.openai_client import MODEL_SCRIPT_GEN, OpenAIClient
from bulkvid.logging import get_logger
from bulkvid.pipeline.open_comments import OpenCommentsAnalysis

_log = get_logger("cartoonprompt")


# ── Constants ────────────────────────────────────────────────────────────────

# Validated cartoon look (spike 2026-06-03): warm, flat, semi-realistic digital
# cartoon illustration. Prepended to every scene prompt so all shots share it.
CARTOON_STYLE = (
    "Flat semi-realistic digital cartoon illustration, warm soft lighting, "
    "clean confident linework, gentle painterly shading, vibrant but natural "
    "colors, modern 2D animated-film look."
)

# Each voiceover targets ~6-7 seconds of speech. At the observed Gemini TTS rate
# (sped up 1.3x downstream) that's roughly this many words.
CARTOON_TARGET_WORDS = 12
CARTOON_MIN_WORDS = 7
CARTOON_MAX_WORDS = 18

DEFAULT_NUM_IDEAS = 2
DEFAULT_NUM_SHOTS = 2
ARTICLE_PROMPT_CHARS = 3_000
DEFAULT_STYLE_DIRECTION = "Read warmly and clearly, like a friendly podcast host."

# Brand-safety clause appended to EVERY image prompt. The image model renders
# recognizable brands by default (e.g. a "car" comes out as a badged VW with a
# readable plate), so this MUST go in the prompt the model actually sees — a
# planner-only rule is not enough. Crucial: no real logos or brands ever.
NO_BRANDING = (
    "Show only generic, unbranded vehicles, products, and signage — absolutely no "
    "real brand names, manufacturer logos, badges, emblems, or hood ornaments, and "
    "no readable license-plate text (leave any plates blank)."
)

# Consistency clause appended to chained (shot 2+) image prompts so the
# image-to-image step keeps the same character and look as the first shot.
CONSISTENCY_CLAUSE = (
    "Keep the SAME main character, outfit, and art style as the reference image."
)


@dataclass
class CartoonShot:
    scene: str          # what the scene shows (no style preamble)
    motion: str         # how it animates (Seedance motion prompt)


@dataclass
class CartoonIdea:
    voiceover: str
    style_direction: str
    shots: list[CartoonShot] = field(default_factory=list)


@dataclass
class CartoonPlan:
    ideas: list[CartoonIdea]
    cost_usd: float


# ── Prompt construction ──────────────────────────────────────────────────────


def _system_prompt(language: str, num_ideas: int, num_shots: int) -> str:
    return (
        "You are a creative director making SHORT animated cartoon social videos "
        "from a news article. You plan the visuals and a tight voiceover.\n\n"
        f"Produce exactly {num_ideas} INDEPENDENT video ideas. Each idea is a "
        f"separate ~6-7 second video told in exactly {num_shots} shots.\n\n"
        "For EACH idea return:\n"
        f"- voiceover: ONE short spoken line in {language or 'the article language'}, "
        f"about {CARTOON_TARGET_WORDS} words ({CARTOON_MIN_WORDS}-{CARTOON_MAX_WORDS}), "
        "natural and engaging, readable in ~6-7 seconds.\n"
        "- style_direction: a short delivery hint for the voice actor.\n"
        f"- shots: an array of exactly {num_shots} shots, each with:\n"
        "    * scene: a vivid description of ONE cartoon scene (subject, setting, "
        "framing). Vertical composition.\n"
        "    * motion: how that scene should gently animate (small, natural "
        "movements and subtle camera moves).\n\n"
        "HARD RULES:\n"
        "1. Use GENERIC, SYMBOLIC characters and objects only. NEVER depict a real, "
        "named, or recognizable public figure. NEVER name a real brand or "
        "manufacturer (e.g. say 'a compact car', NOT 'a Volkswagen'). Describe all "
        "vehicles, products, and signage as plain and unbranded — no logos, badges, "
        "or readable license plates.\n"
        "2. Within one idea, keep ONE recurring main character and describe them "
        "IDENTICALLY across the shots (same age, hair, clothing) so the shots feel "
        "like one continuous scene.\n"
        "3. NO legible on-screen text: keep any screens, signs, phones, or papers "
        "abstract, blurred, or out of focus. Do not ask for words or numbers.\n"
        "4. Keep it tasteful and brand-safe.\n\n"
        'Return STRICT JSON only, shaped exactly like:\n'
        '{"ideas": [{"voiceover": "...", "style_direction": "...", '
        '"shots": [{"scene": "...", "motion": "..."}]}]}'
    )


def _user_message(
    article_body: str, open_comments: OpenCommentsAnalysis
) -> str:
    parts: list[str] = []
    if open_comments.tone_hints:
        parts.append("TONE_HINTS: " + "; ".join(open_comments.tone_hints))
    if open_comments.directives:
        parts.append("DIRECTIVES (honor each): " + "; ".join(open_comments.directives))
    if open_comments.override_script:
        parts.append(
            "PREFERRED VOICEOVER (use or adapt for at least one idea):\n"
            + open_comments.override_script.strip()
        )

    snippet = (article_body or "").strip()[:ARTICLE_PROMPT_CHARS]
    if snippet:
        parts.append("ARTICLE BODY:\n" + snippet)
    else:
        parts.append(
            "ARTICLE BODY: (none provided — invent a generic, on-topic concept)"
        )
    return "\n\n".join(parts)


def image_prompt_for_shot(scene: str, *, is_chained: bool) -> str:
    """Compose the full nano-banana-2 prompt for one shot.

    Prepends the shared cartoon STYLE; for chained (shot 2+) shots, appends the
    consistency clause so the image-to-image step holds the character.
    """
    base = f"{CARTOON_STYLE} {scene.strip()} {NO_BRANDING}"
    return f"{base} {CONSISTENCY_CLAUSE}" if is_chained else base


# ── Fallback ─────────────────────────────────────────────────────────────────


def _fallback_plan(vertical: str, num_ideas: int, num_shots: int) -> list[CartoonIdea]:
    """A generic, on-topic plan so a row still ships if the LLM output is unusable."""
    topic = (vertical or "the topic").strip() or "the topic"
    ideas: list[CartoonIdea] = []
    for _ in range(num_ideas):
        shots = [
            CartoonShot(
                scene=(
                    f"A friendly cartoon character looking thoughtful while "
                    f"considering {topic}, warm everyday setting."
                ),
                motion="Subtle natural movement and a slow, gentle camera push-in.",
            )
            for _ in range(num_shots)
        ]
        ideas.append(
            CartoonIdea(
                voiceover=f"Here's what you should know about {topic} today.",
                style_direction=DEFAULT_STYLE_DIRECTION,
                shots=shots,
            )
        )
    return ideas


def _coerce_ideas(
    raw_ideas: list[Any], num_ideas: int, num_shots: int
) -> list[CartoonIdea]:
    """Normalize the parsed JSON into exactly ``num_ideas`` ideas of ``num_shots`` shots."""
    ideas: list[CartoonIdea] = []
    for raw in raw_ideas[:num_ideas]:
        if not isinstance(raw, dict):
            continue
        voiceover = str(raw.get("voiceover") or "").strip()
        style = str(raw.get("style_direction") or "").strip() or DEFAULT_STYLE_DIRECTION
        raw_shots = raw.get("shots") or []
        shots: list[CartoonShot] = []
        for rs in raw_shots[:num_shots]:
            if not isinstance(rs, dict):
                continue
            scene = str(rs.get("scene") or "").strip()
            motion = str(rs.get("motion") or "").strip() or "Subtle, natural movement."
            if scene:
                shots.append(CartoonShot(scene=scene, motion=motion))
        if voiceover and len(shots) == num_shots:
            ideas.append(CartoonIdea(voiceover=voiceover, style_direction=style, shots=shots))
    return ideas


# ── Public API ───────────────────────────────────────────────────────────────


async def generate_cartoon_plan(
    client: OpenAIClient,
    *,
    article_body: str,
    country: str,
    vertical: str,
    language: str,
    script_pattern: str,
    open_comments: OpenCommentsAnalysis,
    num_ideas: int = DEFAULT_NUM_IDEAS,
    num_shots: int = DEFAULT_NUM_SHOTS,
    model: str = MODEL_SCRIPT_GEN,
) -> CartoonPlan:
    """Plan ``num_ideas`` cartoon videos from the article. Never raises.

    On any parse/shape problem it falls back to a generic on-topic plan so the
    row still ships rather than blocking the batch.
    """
    system = _system_prompt(language, num_ideas, num_shots)
    if script_pattern.strip():
        system += f"\n\nPreferred opening style: {script_pattern.strip()}."
    user = _user_message(article_body, open_comments)

    _log.info(
        "cartoon_plan_submit",
        language=language,
        country=country[:40],
        vertical=vertical[:40],
        num_ideas=num_ideas,
        num_shots=num_shots,
        article_chars=min(len(article_body or ""), ARTICLE_PROMPT_CHARS),
    )

    result = await client.chat(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_tokens=1200,
        temperature=0.8,
    )

    ideas: list[CartoonIdea] = []
    try:
        parsed = json.loads(result.text)
        ideas = _coerce_ideas(parsed.get("ideas") or [], num_ideas, num_shots)
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        _log.error("cartoon_plan_parse_failed", error=str(e), raw_preview=result.text[:200])

    if len(ideas) < num_ideas:
        _log.warning(
            "cartoon_plan_incomplete_filled",
            got=len(ideas),
            wanted=num_ideas,
        )
        ideas += _fallback_plan(vertical, num_ideas - len(ideas), num_shots)

    _log.info(
        "cartoon_plan_ok",
        idea_count=len(ideas),
        cost_usd=result.cost_usd,
    )
    return CartoonPlan(ideas=ideas, cost_usd=result.cost_usd)
