"""Hook_Card copy + scene generator.

One gpt-5.4-mini call (JSON mode) fills only the pieces a hook_card row is
MISSING:

  - ``hook``   — one scroll-stopping headline (<= 120 chars) for the box, used
                 when the sheet's Text cell (col E) is blank.
  - ``scenes`` — N realistic photographic scene descriptions, used only when the
                 row has NO manual images, so the row processor can generate one
                 background per scene.

When both the Text cell and manual images are supplied there is nothing to
generate, so the row processor skips this call entirely (cost 0).

The hook is written in the article's language so a localized market reads
correctly. It is a hook, not ad copy: appealing and curiosity-driven but still
compliant with the major platforms' policies (no unverifiable claims, no
clickbait bait-and-switch, no sensational punctuation / ALL CAPS). Scene
descriptions are plain, photographable, brand-free (rendered by an image model).

Soft-fail by contract: any parse / API problem yields a generic on-topic
fallback so a copy hiccup never blocks the row's video. The char cap and scene
count are re-enforced in code after the model returns.

Plan: ``_plans/2026-07-13-hook-card-tab.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from bulkvid.adapters.openai_client import MODEL_SCRIPT_GEN, OpenAIClient
from bulkvid.logging import get_logger

_log = get_logger("hook_card_copy")


# ── Constants ────────────────────────────────────────────────────────────────

HOOK_MAX_CHARS = 120
MAX_SCENES = 5
ARTICLE_PROMPT_CHARS = 3_000

_SYSTEM_PROMPT = (
    "You are a short-form social-video scriptwriter and art director creating a "
    "faceless vertical video from a news-style article. You return STRICT JSON. "
    "The user tells you which fields to produce; include ONLY those fields.\n\n"
    "\"hook\": ONE scroll-stopping headline for an on-screen text box, written in "
    "the SAME language as the article. It must be concrete and curiosity- or "
    f"benefit-driven, at most {HOOK_MAX_CHARS} characters, one sentence, no "
    "trailing period required. It must comply with the major platforms' policies "
    "(Meta, Google, TikTok): NO unverifiable or absolute claims (no \"best\", "
    "\"#1\", \"guaranteed\", \"cure\", \"miracle\"), NO bait-and-switch clickbait, "
    "NO sensational punctuation (\"!!!\"), NO ALL-CAPS words, NO false urgency.\n\n"
    "\"scenes\": an array of exactly N plain-English descriptions of realistic "
    "photographic scenes that fit the article and vertical — each a concrete "
    "subject, setting, and mood, DISTINCT from the others. They are rendered by "
    "an image model, so: describe photographable things only, include NO "
    "on-image text, signage, logos, or brands, and do NOT restate the hook. Keep "
    "each to one sentence."
)


# ── Data ─────────────────────────────────────────────────────────────────────


@dataclass
class HookCardCopy:
    hook: str = ""
    scenes: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


# ── Helpers ──────────────────────────────────────────────────────────────────


def _truncate_chars(text: str, max_chars: int) -> str:
    """Trim ``text`` to at most ``max_chars``, preferring a word boundary."""
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


def _fallback_hook(vertical: str) -> str:
    topic = (vertical or "this topic").strip() or "this topic"
    return _truncate_chars(f"What you should know about {topic}", HOOK_MAX_CHARS)


def _fallback_scenes(vertical: str, n: int) -> list[str]:
    topic = (vertical or "this topic").strip() or "this topic"
    base = (
        f"A clean, contemporary real-world scene relevant to {topic}, "
        "natural daylight, no text or logos"
    )
    # Light variation so N generated backgrounds are not identical crops.
    angles = [
        "wide establishing shot",
        "close detail shot",
        "over-the-shoulder perspective",
        "eye-level street view",
        "elevated / high-angle view",
    ]
    return [f"{base}, {angles[i % len(angles)]}." for i in range(max(0, n))]


# ── Public API ───────────────────────────────────────────────────────────────


async def generate_hook_card_copy(
    client: OpenAIClient,
    *,
    article_body: str,
    language: str,
    country: str,
    vertical: str,
    open_comments: str,
    want_hook: bool,
    want_scenes: int,
    model: str = MODEL_SCRIPT_GEN,
) -> HookCardCopy:
    """Generate the missing hook and/or ``want_scenes`` scene descriptions.

    ``want_hook`` — produce a hook line (the Text cell was blank).
    ``want_scenes`` — number of scene descriptions to produce (0 when the row
    has manual images). When both are falsy, returns an empty result with no
    API call. Never raises; on any parse / API problem falls back to generic
    on-topic copy so the row still ships.
    """
    want_scenes = max(0, min(want_scenes, MAX_SCENES))
    if not want_hook and want_scenes == 0:
        return HookCardCopy()

    ctx_bits: list[str] = []
    if country.strip():
        ctx_bits.append(f"Target country/market: {country.strip()}")
    if vertical.strip():
        ctx_bits.append(f"Vertical/topic: {vertical.strip()}")
    if language.strip():
        ctx_bits.append(f"Write the hook in: {language.strip()}")
    if open_comments.strip():
        ctx_bits.append(
            "Operator notes/directives (honor them): " + open_comments.strip()[:600]
        )

    wants: list[str] = []
    if want_hook:
        wants.append("\"hook\" (one line)")
    if want_scenes:
        wants.append(f"\"scenes\" (array of exactly {want_scenes})")
    ctx_bits.append("Produce these JSON fields only: " + " and ".join(wants) + ".")

    snippet = (article_body or "").strip()[:ARTICLE_PROMPT_CHARS]
    parts = list(ctx_bits)
    parts.append(
        "ARTICLE BODY:\n" + snippet
        if snippet
        else "ARTICLE BODY: (none provided — invent a generic, on-topic concept)"
    )
    user = "\n\n".join(parts)

    _log.info(
        "hook_card_copy_submit",
        language=language,
        country=country[:40],
        vertical=vertical[:40],
        want_hook=want_hook,
        want_scenes=want_scenes,
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
            max_tokens=500,
            temperature=0.7,
        )
    except Exception as e:    # never fail the row over copy
        _log.warning(
            "hook_card_copy_call_failed",
            error=type(e).__name__,
            detail=str(e)[:200],
        )
        return HookCardCopy(
            hook=_fallback_hook(vertical) if want_hook else "",
            scenes=_fallback_scenes(vertical, want_scenes),
            cost_usd=0.0,
        )

    hook = ""
    scenes: list[str] = []
    try:
        parsed = json.loads(result.text)
        hook = _clean_line(str(parsed.get("hook", "")))
        raw_scenes = parsed.get("scenes", []) or []
        if isinstance(raw_scenes, list):
            scenes = [_clean_line(str(s)) for s in raw_scenes if str(s).strip()]
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        _log.warning(
            "hook_card_copy_parse_failed",
            error=str(e),
            raw_preview=(result.text or "")[:200],
        )

    hook = (
        _truncate_chars(hook or _fallback_hook(vertical), HOOK_MAX_CHARS)
        if want_hook else ""
    )

    if want_scenes:
        fb = _fallback_scenes(vertical, want_scenes)
        # Pad from the fallback if the model returned too few; trim if too many.
        scenes = (scenes + fb)[:want_scenes]
    else:
        scenes = []

    _log.info(
        "hook_card_copy_ok",
        hook_chars=len(hook),
        scene_count=len(scenes),
        cost_usd=result.cost_usd,
    )
    return HookCardCopy(hook=hook, scenes=scenes, cost_usd=result.cost_usd)
