"""Image-VO prompt construction.

Two LLM calls produce the inputs the kie.ai nano-banana-edit model needs:

  1. ``describe_source_image`` — gpt-4o reads the user's seed image and
     returns a structured visual description (subject, setting, style,
     colors, composition, story potential). Same as the production
     ``gpt4o_describe_image`` in ``refs/CBImageNoText`` lines 45-103.

  2. ``build_collage_prompt`` — gpt-5.4-mini turns that description into
     a 2x2 collage editing prompt for nano-banana-edit. Upgraded from the
     production gpt-4.1-mini per Yoav's directive (plan §5).

Both calls return ``(text, cost_usd)`` so the row processor can sum cost.

Plan: ``_plans/2026-06-02-aporia-bulk-video-tool.md`` §3 (mandatory collage
method), §5 (models), §11 (cost), §15 Appendix D.
"""

from __future__ import annotations

from bulkvid.adapters.openai_client import (
    MODEL_COLLAGE_PROMPT,
    MODEL_VISION,
    OpenAIClient,
)
from bulkvid.logging import get_logger

_log = get_logger("imageprompt")


_DESCRIBE_PROMPT = (
    "You are a senior advertising creative director analysing an inspiration ad "
    "image so another model can create NEW ad images in the same spirit.\n\n"
    "Cover these points concisely:\n"
    "1. SUBJECT: the main subject or product.\n"
    "2. SETTING: where the scene takes place.\n"
    "3. STYLE: photographic, illustrated, realistic, etc., plus the dominant color "
    "palette and mood.\n"
    "4. COMPOSITION: how the scene is framed and where any on-image text sits.\n"
    "5. MARKETING TEXT: read ALL on-image text precisely — the headline, any "
    "sub-text, and the call-to-action (CTA). Quote it verbatim and state the language.\n"
    "6. MESSAGE: in one line, what the ad is selling and its persuasion angle.\n"
    "7. BRANDING: note any logos, badges, signage, brand names, or brand marks "
    "present, ONLY so they can be AVOIDED — the generated images must contain NO "
    "real brands.\n\n"
    "Return only the structured description, no preamble, no commentary."
)


_COLLAGE_SYSTEM = (
    "You are a world-class advertising creative director who writes ultra-precise "
    "image prompts for text-capable models (Nano Banana 2 / GPT Image 2). You "
    "produce 2x2 grid collages where each panel is a finished, scroll-stopping "
    "vertical ad frame with a legible marketing headline and call-to-action."
)


def _collage_user_message(description: str, article_excerpt: str = "") -> str:
    article_block = (
        "ARTICLE CONTEXT (the new photos must depict subjects relevant to THIS "
        "article — not necessarily whatever the inspiration photo happened to "
        f"show):\n{article_excerpt.strip()}\n\n"
        if article_excerpt.strip()
        else ""
    )
    return (
        f"Inspiration ad analysis:\n{description}\n\n"
        f"{article_block}"
        "Create ONE image that is a 2x2 GRID of exactly 4 equal-sized panels:\n"
        "  TOP-LEFT = Panel 1 | TOP-RIGHT = Panel 2\n"
        "  BOTTOM-LEFT = Panel 3 | BOTTOM-RIGHT = Panel 4\n\n"
        "GOAL: keep the inspiration ad's TEXT and layout, change ONLY the photo.\n\n"
        "KEEP THE TEXT (do NOT rewrite):\n"
        "- Reuse the inspiration's headline and call-to-action EXACTLY as written "
        "(verbatim, same language), keeping the SAME text layout, banner/label "
        "style, and placement. Use the SAME headline and CTA on all 4 panels.\n"
        "- The ONLY change allowed to the text is replacing a real brand or company "
        "name with a generic term.\n"
        "- Text must stay crisp, correctly spelled and legible.\n\n"
        "CHANGE ONLY THE PHOTO:\n"
        "- Replace the inspiration's photo/visual with a NEW, realistic photo that "
        "fits the article context above (or, with no article context, the same "
        "product/subject as the inspiration). Vary the photo across the 4 panels "
        "(different scenes / angles / examples) so they read as a varied ad set.\n"
        "- Keep the overall ad design, colours and mood consistent with the inspiration.\n\n"
        "NO REAL BRANDS (strict — legal requirement):\n"
        "- The imagery must contain NO real brand logos, trademarks, brand names, "
        "badges, or recognisable branding — not even if the inspiration shows them.\n"
        "- Show any product as a generic, unbranded item.\n\n"
        "LAYOUT RULES (must follow):\n"
        "- ONE image, 2 columns x 2 rows, 4 equal panels, thin neutral divider between them.\n"
        "- Do NOT stack panels into a single column. Do NOT repeat the same panel.\n\n"
        "FORMAT your response exactly like this and nothing else:\n"
        "Create a 2x2 grid collage (2 columns, 2 rows, 4 equal panels), each a vertical "
        "ad frame that REUSES the inspiration's exact headline and CTA and only changes the photo.\n"
        'Headline on every panel (verbatim from the inspiration): "[exact headline]".\n'
        'CTA on every panel (verbatim from the inspiration): "[exact cta]".\n'
        "TOP-LEFT panel photo: [new article-relevant scene].\n"
        "TOP-RIGHT panel photo: [new article-relevant scene].\n"
        "BOTTOM-LEFT panel photo: [new article-relevant scene].\n"
        "BOTTOM-RIGHT panel photo: [new article-relevant scene].\n"
        "All panels: keep the inspiration's text, banner style and overall look; only the "
        "photo changes; text legible and correctly spelled; no real brands."
    )


# ── Public API ───────────────────────────────────────────────────────────────


async def describe_source_image(
    client: OpenAIClient,
    image_b64: str,
    model: str = MODEL_VISION,
) -> tuple[str, float]:
    """GPT-4o describes a seed image. Returns ``(description, cost_usd)``."""
    _log.info("describe_submit", b64_chars=len(image_b64))
    result = await client.vision_describe(
        prompt=_DESCRIBE_PROMPT,
        image_b64=image_b64,
        model=model,
        detail="high",
        max_tokens=500,
    )
    desc = result.text.strip()
    if not desc:
        _log.warning("describe_empty_response")
        desc = "An advertising photograph. Subject not clearly identified."
    _log.info(
        "describe_ok",
        chars=len(desc),
        cost_usd=result.cost_usd,
    )
    return desc, result.cost_usd


async def build_collage_prompt(
    client: OpenAIClient,
    description: str,
    model: str = MODEL_COLLAGE_PROMPT,
    article_excerpt: str = "",
) -> tuple[str, float]:
    """gpt-5.4-mini builds the 2x2 collage prompt. Returns ``(prompt, cost_usd)``.

    Keeps the inspiration's headline + CTA + layout verbatim and changes ONLY the
    photo; ``article_excerpt`` (when given) grounds the new photo in the article
    topic rather than whatever the inspiration photo happened to show."""
    _log.info(
        "collage_prompt_submit",
        description_chars=len(description),
        article_chars=len(article_excerpt),
    )
    result = await client.chat(
        model=model,
        messages=[
            {"role": "system", "content": _COLLAGE_SYSTEM},
            {"role": "user", "content": _collage_user_message(description, article_excerpt)},
        ],
        max_tokens=800,
        temperature=0.7,
    )
    text = result.text.strip()
    # Strip wrapping quotes if the model added them.
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        text = text[1:-1]
    _log.info(
        "collage_prompt_ok",
        chars=len(text),
        cost_usd=result.cost_usd,
    )
    return text, result.cost_usd
