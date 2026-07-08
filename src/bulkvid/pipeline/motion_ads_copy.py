"""Motion_Ads copy + scene generator.

One gpt-5.4-mini call (JSON mode) turns an article into everything the
Motion_Ads tab needs that ISN'T the video itself:

  - ``headline``     — a short marketing headline (<= 60 chars) for sheet col D
  - ``description``  — a one-line description (<= 80 chars) for sheet col E
  - ``image_scene``  — a concrete, realistic photographic scene description that
                       the row processor turns into a nano-banana-2 image prompt

Headline + description are ad-copy: appealing but written to pass the major ad
platforms' policies (no unverifiable claims, no clickbait, no sensational
punctuation / ALL CAPS, no false urgency). Both are produced in the article's
detected language so a DE / NL / SE market reads correctly (localization).

Soft-fail by contract: any parse / API problem yields a generic on-topic
fallback derived from the vertical so a copy hiccup never blocks the row's
video. Char caps are re-enforced in code after the model returns — the prompt
asks for the limits, this guarantees them.

Plan: ``_plans/2026-07-08-motion-ads-tab.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from bulkvid.adapters.openai_client import MODEL_SCRIPT_GEN, OpenAIClient
from bulkvid.logging import get_logger

_log = get_logger("motion_ads_copy")


# ── Constants ────────────────────────────────────────────────────────────────

HEADLINE_MAX_CHARS = 60
DESCRIPTION_MAX_CHARS = 80
ARTICLE_PROMPT_CHARS = 3_000

_SYSTEM_PROMPT = (
    "You are a senior advertising copywriter and art director creating a native "
    "motion-ad from a news-style article. You return STRICT JSON with exactly "
    "three string fields: \"headline\", \"description\", \"image_scene\".\n\n"
    "Write the headline and description in the SAME language as the article. "
    "They must be appealing but comply with the major ad platforms' policies "
    "(Google, Meta, Taboola):\n"
    f"- headline: a concrete marketing hook, at most {HEADLINE_MAX_CHARS} "
    "characters.\n"
    f"- description: one supporting line, at most {DESCRIPTION_MAX_CHARS} "
    "characters.\n"
    "- NO unverifiable or absolute claims (no \"best\", \"#1\", \"guaranteed\", "
    "\"cure\", \"miracle\"), NO clickbait or curiosity-gap teasing, NO "
    "sensational punctuation (\"!!!\"), NO ALL-CAPS words, NO false urgency or "
    "fake scarcity, NO medical / financial promises.\n\n"
    "\"image_scene\" is a plain-English description of ONE realistic photographic "
    "scene that fits the article and vertical — concrete subject, setting, and "
    "mood. It will be rendered by an image model, so: describe photographable "
    "things only, include NO on-image text, signage, logos, or brands, and do "
    "NOT restate the headline. Keep it to one or two sentences."
)


# ── Data ─────────────────────────────────────────────────────────────────────


@dataclass
class MotionAdsCopy:
    headline: str
    description: str
    image_scene: str
    cost_usd: float


# ── Helpers ──────────────────────────────────────────────────────────────────


def _truncate_chars(text: str, max_chars: int) -> str:
    """Trim ``text`` to at most ``max_chars``, preferring a word boundary.

    Cuts at the last space inside the cap so a headline never ends mid-word;
    falls back to a hard cut when there's no space (one very long token).
    """
    t = (text or "").strip()
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    space = cut.rfind(" ")
    if space >= max_chars // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-").rstrip()


def _clean_line(text: str) -> str:
    """Strip wrapping quotes + surrounding whitespace from a model line."""
    t = (text or "").strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("\"", "'"):
        t = t[1:-1].strip()
    return t


def _fallback(vertical: str) -> tuple[str, str, str]:
    """Generic, on-topic copy + scene so a row still ships if the LLM fails."""
    topic = (vertical or "this topic").strip() or "this topic"
    headline = _truncate_chars(f"What to know about {topic}", HEADLINE_MAX_CHARS)
    description = _truncate_chars(
        f"A closer look at {topic} and why it matters now.",
        DESCRIPTION_MAX_CHARS,
    )
    scene = (
        f"A clean, contemporary real-world scene relevant to {topic}, "
        "natural daylight, no text or logos."
    )
    return headline, description, scene


# ── Public API ───────────────────────────────────────────────────────────────


async def generate_motion_ads_copy(
    client: OpenAIClient,
    *,
    article_body: str,
    language: str,
    country: str,
    vertical: str,
    open_comments: str,
    apple: bool,
    model: str = MODEL_SCRIPT_GEN,
) -> MotionAdsCopy:
    """Generate ``{headline, description, image_scene}`` for one Motion_Ads row.

    Never raises. On any parse / API problem returns a generic on-topic
    fallback (cost carried through where a call was actually made). Char caps
    are enforced here regardless of what the model returns.

    ``apple`` — when True, the scene is additionally constrained to contain no
    people (the row processor ALSO appends a hard no-people clause to the image
    prompt; this keeps the scene itself people-free so the two agree).
    """
    ctx_bits: list[str] = []
    if country.strip():
        ctx_bits.append(f"Target country/market: {country.strip()}")
    if vertical.strip():
        ctx_bits.append(f"Vertical/topic: {vertical.strip()}")
    if language.strip():
        ctx_bits.append(f"Write the headline and description in: {language.strip()}")
    if open_comments.strip():
        ctx_bits.append(
            "Operator notes/directives (honor them): " + open_comments.strip()[:600]
        )
    if apple:
        ctx_bits.append(
            "IMPORTANT: the image_scene must contain NO people — no humans, "
            "faces, hands, or body parts. Show only objects, products, "
            "environments, or scenery."
        )

    snippet = (article_body or "").strip()[:ARTICLE_PROMPT_CHARS]
    parts = list(ctx_bits)
    parts.append(
        "ARTICLE BODY:\n" + snippet
        if snippet
        else "ARTICLE BODY: (none provided — invent a generic, on-topic concept)"
    )
    user = "\n\n".join(parts)

    _log.info(
        "motion_ads_copy_submit",
        language=language,
        country=country[:40],
        vertical=vertical[:40],
        apple=apple,
        article_chars=len(snippet),
    )

    try:
        result = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            max_tokens=400,
            temperature=0.6,
        )
    except Exception as e:    # never fail the row over copy
        _log.warning(
            "motion_ads_copy_call_failed",
            error=type(e).__name__,
            detail=str(e)[:200],
        )
        headline, description, scene = _fallback(vertical)
        return MotionAdsCopy(headline, description, scene, 0.0)

    headline = ""
    description = ""
    scene = ""
    try:
        parsed = json.loads(result.text)
        headline = _clean_line(str(parsed.get("headline", "")))
        description = _clean_line(str(parsed.get("description", "")))
        scene = _clean_line(str(parsed.get("image_scene", "")))
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        _log.warning(
            "motion_ads_copy_parse_failed",
            error=str(e),
            raw_preview=(result.text or "")[:200],
        )

    fb_headline, fb_description, fb_scene = _fallback(vertical)
    headline = _truncate_chars(headline or fb_headline, HEADLINE_MAX_CHARS)
    description = _truncate_chars(description or fb_description, DESCRIPTION_MAX_CHARS)
    scene = scene or fb_scene

    _log.info(
        "motion_ads_copy_ok",
        headline_chars=len(headline),
        description_chars=len(description),
        scene_chars=len(scene),
        cost_usd=result.cost_usd,
    )
    return MotionAdsCopy(
        headline=headline,
        description=description,
        image_scene=scene,
        cost_usd=result.cost_usd,
    )
