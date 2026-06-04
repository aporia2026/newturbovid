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
from bulkvid.orchestrator.runtime_settings import SETTING_SENSITIVE_APPAREL_RULES
from bulkvid.orchestrator.settings_store import SettingsStore
from bulkvid.pipeline.safety import SAFE, SafetyContext, append_safety_block

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
        "Produce ONE image that is a STRICT 2x2 GRID — 2 equal columns and 2 equal "
        "rows = 4 cells of IDENTICAL size that tile perfectly:\n"
        "  TOP-LEFT = Panel 1 | TOP-RIGHT = Panel 2\n"
        "  BOTTOM-LEFT = Panel 3 | BOTTOM-RIGHT = Panel 4\n"
        "The grid lines sit EXACTLY at the horizontal and vertical centre, so the "
        "image splits cleanly into 4 equal quarters with a thin neutral divider.\n\n"
        "Each cell is its OWN complete, self-contained vertical ad frame. CRITICAL: "
        "do NOT draw one big ad across the whole image, do NOT stack ads in a single "
        "column, and NEVER let an ad or its text span more than one cell.\n\n"
        "KEEP THE TEXT (do NOT rewrite):\n"
        "- Put the inspiration's headline and call-to-action on EACH cell, EXACTLY as "
        "written (verbatim, same language), in the same banner/label visual style. "
        "Use the SAME headline and CTA on all 4 cells.\n"
        "- The ONLY change allowed to the text is replacing a real brand or company "
        "name with a generic term. Keep text crisp, correctly spelled and legible.\n\n"
        "CHANGE ONLY THE PHOTO:\n"
        "- In each cell, replace the inspiration's photo with a NEW, realistic photo "
        "that fits the article context above (or, with no article context, the same "
        "product/subject as the inspiration). Vary the photo across the 4 cells.\n\n"
        "NO REAL BRANDS (strict — legal requirement):\n"
        "- The imagery must contain NO real brand logos, trademarks, brand names, "
        "badges, or recognisable branding — not even if the inspiration shows them.\n\n"
        "FORMAT your response exactly like this and nothing else:\n"
        "Create a single image that is a STRICT 2x2 grid (2 equal columns, 2 equal rows, "
        "4 identical-size cells, thin neutral divider), each cell a complete vertical ad "
        "that reuses the inspiration's exact headline and CTA and only changes the photo.\n"
        'Headline on every cell (verbatim): "[exact headline]".\n'
        'CTA on every cell (verbatim): "[exact cta]".\n'
        "TOP-LEFT cell photo: [new article-relevant scene].\n"
        "TOP-RIGHT cell photo: [new article-relevant scene].\n"
        "BOTTOM-LEFT cell photo: [new article-relevant scene].\n"
        "BOTTOM-RIGHT cell photo: [new article-relevant scene].\n"
        "The 4 cells are equal and tile perfectly; no single full-image ad; no stacking; "
        "text legible and correctly spelled; no real brands."
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
    *,
    settings_store: SettingsStore | None = None,
    safety: SafetyContext = SAFE,
) -> tuple[str, float]:
    """gpt-5.4-mini builds the 2x2 collage prompt. Returns ``(prompt, cost_usd)``.

    Keeps the inspiration's headline + CTA + layout verbatim and changes ONLY the
    photo; ``article_excerpt`` (when given) grounds the new photo in the article
    topic rather than whatever the inspiration photo happened to show.

    When ``safety.matched`` the admin's sensitive-apparel safety block is
    appended to the user message before the LLM call, so the generated image
    description forces product-only frames with no humans.
    """
    user_message = _collage_user_message(description, article_excerpt)
    if safety.matched and settings_store is not None:
        # No explicit default — let the store fall through to the registered
        # default (``SENSITIVE_APPAREL_RULES_DEFAULT``).
        safety_block = await settings_store.get(SETTING_SENSITIVE_APPAREL_RULES)
        user_message = append_safety_block(user_message, safety, safety_block)
        if safety_block.strip():
            _log.info(
                "safety_applied",
                stage="collage_prompt",
                matched_keyword=safety.matched_keyword,
            )

    _log.info(
        "collage_prompt_submit",
        description_chars=len(description),
        article_chars=len(article_excerpt),
        safety_matched=safety.matched,
    )
    result = await client.chat(
        model=model,
        messages=[
            {"role": "system", "content": _COLLAGE_SYSTEM},
            {"role": "user", "content": user_message},
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
