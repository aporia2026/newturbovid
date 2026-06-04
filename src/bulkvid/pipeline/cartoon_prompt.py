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
from bulkvid.orchestrator.runtime_settings import (
    CARTOON_PLANNER_PROMPT_DEFAULT,
    SETTING_CARTOON_PLANNER_PROMPT,
    SETTING_SENSITIVE_APPAREL_RULES,
)
from bulkvid.orchestrator.settings_store import SettingsStore
from bulkvid.pipeline.open_comments import OpenCommentsAnalysis
from bulkvid.pipeline.safety import SAFE, SafetyContext, append_safety_block

_log = get_logger("cartoonprompt")


# ── Constants ────────────────────────────────────────────────────────────────

# Validated cartoon look (spike 2026-06-03): warm, flat, semi-realistic digital
# cartoon illustration. Prepended to every scene prompt so all shots share it.
CARTOON_STYLE = (
    "Flat semi-realistic digital cartoon illustration, warm soft lighting, "
    "clean confident linework, gentle painterly shading, vibrant but natural "
    "colors, modern 2D animated-film look."
)

# Each voiceover targets ~6-7 seconds of speech. The observed Gemini TTS rate
# (after the 1.3x downstream speed-up) varies 1.5-3.5 wps across live runs on
# 2026-06-03 — the model's free-form ``style_direction`` swings delivery between
# calm-deliberate and punchy-fast. With that variance pinned by the row
# processor's [4, 8]s output clamp + 0.8s tail silence, the word range is now
# set to lift natural speech length: at the median ~2 wps, 13 words ≈ 6.5s
# (centre of target); at the slow end 11 × 0.67 ≈ 7.3s; at the fast end 15
# words still produces a ~4s VO that the soft tail rounds out without dead air.
CARTOON_TARGET_WORDS = 13
CARTOON_MIN_WORDS = 11
CARTOON_MAX_WORDS = 15

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


def _format_planner_prompt(
    template: str, *, language: str, num_ideas: int, num_shots: int
) -> str:
    """Substitute the planner's per-row placeholders into the admin template.

    Tolerant of unknown ``{...}`` tokens — admin edits may drop a placeholder
    or paste literal braces in examples. KeyError falls through to a one-by-one
    replacement that leaves unknown tokens as-is.
    """
    vars: dict[str, object] = {
        "language": language or "the article language",
        "num_ideas": num_ideas,
        "num_shots": num_shots,
        "target_words": CARTOON_TARGET_WORDS,
        "min_words": CARTOON_MIN_WORDS,
        "max_words": CARTOON_MAX_WORDS,
    }
    try:
        return template.format(**vars)
    except (KeyError, IndexError) as e:
        _log.warning(
            "cartoon_planner_substitution_warning",
            error=str(e),
            note="missing placeholder; using literal fallback",
        )
        out = template
        for k, v in vars.items():
            out = out.replace("{" + k + "}", str(v))
        return out


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


def _enforce_word_cap(text: str, max_words: int) -> str:
    """Truncate ``text`` to at most ``max_words`` words.

    Prefers to end at the last sentence boundary (``.``, ``!``, ``?``) inside the
    cap so the spoken line doesn't end mid-thought; otherwise trims at the word
    boundary and adds a terminal period. Backstop for model drift past the
    prompt's word range — see CARTOON_MAX_WORDS rationale above.
    """
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    truncated = " ".join(words[:max_words])
    half = len(truncated) // 2
    last_break = max(truncated.rfind(p) for p in ".!?")
    if last_break >= half:
        return truncated[: last_break + 1].rstrip()
    return truncated.rstrip(",;:- ").rstrip() + "."


def _first_nonempty(d: dict, *keys: str) -> str:
    """Return the first present-and-nonblank string value across ``keys``.

    Lets the coercer accept shape drift from the model (``voice_over`` instead
    of ``voiceover``, ``description`` instead of ``scene``, etc.) without
    rewriting the same defensive lookup at every call site.
    """
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


_DEFAULT_MOTION = "Subtle, natural movement."


def _coerce_ideas(
    raw_ideas: list[Any], num_ideas: int, num_shots: int
) -> list[CartoonIdea]:
    """Normalize parsed JSON into exactly ``num_ideas`` ideas of ``num_shots`` shots.

    Defensive against the gpt-5.4-mini planner's shape drift (live runs have
    seen alternate key names and short/long shot lists):
      - voiceover may be under voiceover / voice_over / vo / line / script /
        narration
      - shots may be under shots / scenes / sequence (and shots can be bare
        strings instead of {scene, motion} dicts)
      - scene may be under scene / description / visual / image / prompt
      - motion may be under motion / action / animation / movement
      - shot lists too short are padded by repeating the last valid shot
      - shot lists too long are trimmed to ``num_shots``

    Anything that still can't yield a voiceover + at least one valid scene is
    rejected, with the reason logged at debug for diagnosis.
    """
    ideas: list[CartoonIdea] = []
    for raw_idx, raw in enumerate(raw_ideas[:num_ideas]):
        if not isinstance(raw, dict):
            _log.debug(
                "cartoon_idea_rejected",
                idea_index=raw_idx, reason="not_a_dict",
                type=type(raw).__name__,
            )
            continue
        voiceover_raw = _first_nonempty(
            raw, "voiceover", "voice_over", "vo", "line", "script", "narration"
        )
        if not voiceover_raw:
            _log.debug(
                "cartoon_idea_rejected",
                idea_index=raw_idx, reason="no_voiceover",
                keys=list(raw.keys())[:10],
            )
            continue
        original_words = len(voiceover_raw.split())
        voiceover = _enforce_word_cap(voiceover_raw, CARTOON_MAX_WORDS)
        if original_words > CARTOON_MAX_WORDS:
            _log.warning(
                "cartoon_voiceover_capped",
                idea_index=raw_idx,
                original_words=original_words,
                max_words=CARTOON_MAX_WORDS,
                capped_words=len(voiceover.split()),
            )
        style = (
            _first_nonempty(raw, "style_direction", "style", "tone", "delivery")
            or DEFAULT_STYLE_DIRECTION
        )

        raw_shots = raw.get("shots") or raw.get("scenes") or raw.get("sequence") or []
        if not isinstance(raw_shots, list):
            raw_shots = []
        shots: list[CartoonShot] = []
        for rs in raw_shots:
            if isinstance(rs, str):
                scene = rs.strip()
                if scene:
                    shots.append(CartoonShot(scene=scene, motion=_DEFAULT_MOTION))
                continue
            if not isinstance(rs, dict):
                continue
            scene = _first_nonempty(rs, "scene", "description", "visual", "image", "prompt")
            motion = (
                _first_nonempty(rs, "motion", "action", "animation", "movement")
                or _DEFAULT_MOTION
            )
            if scene:
                shots.append(CartoonShot(scene=scene, motion=motion))

        if not shots:
            _log.debug(
                "cartoon_idea_rejected",
                idea_index=raw_idx, reason="no_valid_shots",
                keys=list(raw.keys())[:10],
            )
            continue
        # Pad short lists by repeating the last valid shot (image-to-image
        # chaining keeps the visual cohesive even when the scenes are similar),
        # and trim long lists down to the requested count.
        while len(shots) < num_shots:
            shots.append(shots[-1])
        shots = shots[:num_shots]
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
    settings_store: SettingsStore | None = None,
    safety: SafetyContext = SAFE,
) -> CartoonPlan:
    """Plan ``num_ideas`` cartoon videos from the article. Never raises.

    On any parse/shape problem it falls back to a generic on-topic plan so the
    row still ships rather than blocking the batch.

    The planner system prompt is admin-editable via the ``cartoon_planner_prompt``
    setting; when ``safety.matched`` the sensitive-apparel block is appended
    so both the voiceover and the scene descriptions stay product-only.
    """
    template = CARTOON_PLANNER_PROMPT_DEFAULT
    safety_block = ""
    if settings_store is not None:
        template = await settings_store.get(
            SETTING_CARTOON_PLANNER_PROMPT, default=CARTOON_PLANNER_PROMPT_DEFAULT
        )
        if safety.matched:
            # No explicit default — let the store fall through to the
            # registered default (``SENSITIVE_APPAREL_RULES_DEFAULT``).
            safety_block = await settings_store.get(
                SETTING_SENSITIVE_APPAREL_RULES
            )

    system = _format_planner_prompt(
        template,
        language=language,
        num_ideas=num_ideas,
        num_shots=num_shots,
    )
    if script_pattern.strip():
        system += f"\n\nPreferred opening style: {script_pattern.strip()}."
    system = append_safety_block(system, safety, safety_block)
    if safety.matched:
        _log.info(
            "safety_applied",
            stage="cartoon_planner_prompt",
            matched_keyword=safety.matched_keyword,
        )
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
            raw_preview=result.text[:300],
        )
        ideas += _fallback_plan(vertical, num_ideas - len(ideas), num_shots)

    _log.info(
        "cartoon_plan_ok",
        idea_count=len(ideas),
        cost_usd=result.cost_usd,
    )
    return CartoonPlan(ideas=ideas, cost_usd=result.cost_usd)
