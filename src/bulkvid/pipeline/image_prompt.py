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


def _collage_user_message(
    description: str,
    article_excerpt: str = "",
    cta_override: str = "",
) -> str:
    """Default (with-text) collage prompt for the legacy image_vo path.

    Hard-constrains every cell to the THREE-BAND LAYOUT Yoav specified
    2026-06-08 as the canonical default look: solid-white headline band at
    top, photo in the middle, solid-white CTA band at bottom. Same layout
    every cell, every run, regardless of what the inspiration seed image
    happened to look like — earlier prompts let the seed's banner style
    drive the output and produced inconsistent results across rows.

    ``cta_override`` (when non-empty): the CTA band must use this EXACT
    text on every cell. Replaces — does NOT append to — the per-language
    Read-More guidance, so kie sees only one CTA instruction and doesn't
    render both the override AND the per-language fallback stacked.
    """
    article_block = (
        "ARTICLE CONTEXT (the headline and the new photos must reflect THIS "
        "article — not whatever the inspiration photo happened to show):\n"
        f"{article_excerpt.strip()}\n\n"
        if article_excerpt.strip()
        else ""
    )
    return (
        f"Inspiration ad analysis (use ONLY for color/photo-style cues — its "
        f"layout is NOT the target):\n{description}\n\n"
        f"{article_block}"
        "Produce ONE image that is a STRICT 2x2 GRID — 2 equal columns and 2 equal "
        "rows = 4 cells of IDENTICAL size that tile perfectly:\n"
        "  TOP-LEFT = Panel 1 | TOP-RIGHT = Panel 2\n"
        "  BOTTOM-LEFT = Panel 3 | BOTTOM-RIGHT = Panel 4\n"
        "The grid lines sit EXACTLY at the horizontal and vertical centre, so the "
        "image splits cleanly into 4 equal quarters with a thin neutral divider.\n\n"
        "MANDATORY 3-BAND LAYOUT FOR EVERY CELL (strict — every cell must look "
        "the same shape; this is not optional and overrides any layout the "
        "inspiration ad happened to use):\n"
        "  • TOP BAND ≈ 26% of cell height — SOLID WHITE background, edge-to-edge. "
        "Bold black sans-serif headline text, large, multi-line wrap if needed, "
        "horizontally centered, comfortably padded from cell edges. NO drop "
        "shadows, NO color tint, NO decorative borders, NO graphic elements — "
        "just black text on solid white.\n"
        "  • MIDDLE BAND ≈ 60% of cell height — a realistic photograph relevant "
        "to the article. Photographic content only; NO overlaid text, NO logos, "
        "NO captions, NO banners over the photo.\n"
        "  • BOTTOM BAND ≈ 14% of cell height — SOLID WHITE background, edge-to-"
        "edge. Bold black sans-serif CTA text, horizontally centered, comfortably "
        "padded. Same plain styling as the top band — black text on solid white.\n\n"
        "Each cell is its OWN self-contained vertical ad frame in this 3-band "
        "shape. CRITICAL: do NOT make the photo full-bleed across the cell, do "
        "NOT skip the white bands, do NOT make the bands semi-transparent or "
        "tinted, do NOT put text on the photo itself.\n\n"
        "HEADLINE TEXT (top band — same headline on ALL 4 cells):\n"
        "- Write a concise marketing headline in the ARTICLE'S language (the "
        "language the article excerpt above is written in).\n"
        "- Length ≈ 8-14 words; may wrap to 2-4 lines.\n"
        "- Summarizes the article topic concretely (concrete nouns, no fluff).\n"
        "- Crisp, correctly spelled, legible.\n\n"
        + (
            # STRICT override path — only this CTA, nothing else. Repeated
            # phrasing on purpose so kie can't merge it with any other
            # CTA-related instruction earlier or later in the prompt.
            "CTA TEXT (bottom band — STRICT, same on ALL 4 cells):\n"
            f"- The CTA text on every cell is EXACTLY: \"{cta_override.strip()}\".\n"
            "- Do NOT translate, paraphrase, expand, abbreviate, or reword it.\n"
            "- Do NOT add a second CTA, secondary text line, or any other "
            "call-to-action above, below, or beside it — only the EXACT "
            "single line above appears on the bottom band.\n"
            "- Crisp, correctly spelled, legible, in bold black sans-serif.\n\n"
            if cta_override.strip()
            else
            "CTA TEXT (bottom band — same CTA on ALL 4 cells):\n"
            "- A generic \"Read More\" style call-to-action in the article's language.\n"
            "- One short line, ideally 1-3 words. Examples by language: English "
            "\"Read More\"; German \"Weiterlesen\"; Spanish \"Saber Más\"; French "
            "\"En Savoir Plus\"; Italian \"Scopri di Più\"; Portuguese \"Saiba Mais\"; "
            "Dutch \"Meer Weten\"; Polish \"Dowiedz Się Więcej\"; Hebrew \"למידע נוסף\".\n\n"
        ) +
        "PHOTOS (middle band — VARY across the 4 cells):\n"
        "- Realistic, photographic, contemporary. Relevant to the article topic.\n"
        "- DIFFERENT photo per cell so the 4 generated videos don't feel "
        "interchangeable. Same subject area, different angles/scenes/moments.\n\n"
        "NO REAL BRANDS (strict — legal requirement):\n"
        "- The photos must contain NO real brand logos, trademarks, brand names, "
        "badges, or recognisable branding — not even if the inspiration shows them.\n"
        "- License plates, signage, product labels visible in the photo MUST be "
        "generic, blurred, or removed.\n\n"
        "FORMAT your response exactly like this and nothing else:\n"
        "Create a single image that is a STRICT 2x2 grid (2 equal columns, 2 equal rows, "
        "4 identical-size cells, thin neutral divider). EACH cell follows the same 3-band "
        "layout: solid-white top band (~26% height) with a bold black sans-serif headline, "
        "photographic middle band (~60% height), solid-white bottom band (~14% height) with "
        + (
            f'the exact CTA "{cta_override.strip()}" in bold black sans-serif. '
            if cta_override.strip()
            else 'a bold black sans-serif "Read More" CTA in the article\'s language. '
        )
        + "Same headline "
        "and CTA on every cell; vary the middle photo per cell.\n"
        'Headline (verbatim, same on every cell, in the article\'s language): "[8-14 word headline]".\n'
        + (
            f'CTA (verbatim, same on every cell — STRICT EXACT TEXT, no translation): "{cta_override.strip()}".\n'
            if cta_override.strip()
            else 'CTA (verbatim, same on every cell, in the article\'s language): "[short Read-More CTA]".\n'
        )
        + "TOP-LEFT cell photo: [new article-relevant scene, photographic, no text].\n"
        "TOP-RIGHT cell photo: [different article-relevant scene, photographic, no text].\n"
        "BOTTOM-LEFT cell photo: [different article-relevant scene, photographic, no text].\n"
        "BOTTOM-RIGHT cell photo: [different article-relevant scene, photographic, no text].\n"
        "Every cell uses the 3-band layout (white-top, photo-middle, white-bottom); white "
        "bands are SOLID white with bold black sans-serif text; no text overlays on the "
        "photo; no real brands; cells tile perfectly with a thin neutral divider."
    )


def _collage_user_message_no_text(description: str, article_excerpt: str = "") -> str:
    """Same 2x2 grid request as the default, but the caller will draw the
    headline + CTA themselves with Pillow later — so we explicitly ask kie
    to leave the cells text-free. Otherwise the Pillow overlay would sit on
    top of kie-baked text and produce visible double-text artifacts.

    Used by the simple_x4 row processor when at least one card on the row
    has a chosen template_id. Plan
    ``_plans/2026-06-08-simple-x4-template-cards.md`` §D.2 (R1).
    """
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
        "Each cell is its OWN complete, self-contained vertical photo. CRITICAL: "
        "do NOT draw one big photo across the whole image, do NOT stack photos in a "
        "single column.\n\n"
        "NO TEXT ON THE PHOTOS — STRICT:\n"
        "- Do NOT draw any headline, sub-text, call-to-action, button, label, "
        "caption, sticker, or any other written/typeset words on the cells.\n"
        "- Do NOT include legible signage, license plates, document text, screen "
        "text, banner text, or any other readable words in the photographic content.\n"
        "- The cells must be CLEAN PHOTOS only — every text overlay will be added "
        "afterwards in post-processing.\n"
        "- IGNORE the inspiration ad's headline and CTA entirely; do not reproduce "
        "them. They are NOT part of this output.\n\n"
        "PHOTOGRAPHIC CONTENT:\n"
        "- In each cell, produce a realistic photo that fits the article context "
        "above (or, with no article context, the same subject as the inspiration). "
        "Vary the photo across the 4 cells.\n\n"
        "NO REAL BRANDS (strict — legal requirement):\n"
        "- The imagery must contain NO real brand logos, trademarks, brand names, "
        "badges, or recognisable branding — not even if the inspiration shows them.\n\n"
        "FORMAT your response exactly like this and nothing else:\n"
        "Create a single image that is a STRICT 2x2 grid (2 equal columns, 2 equal rows, "
        "4 identical-size cells, thin neutral divider). Each cell is a clean realistic "
        "photo with NO text, NO headline, NO CTA, NO captions, NO signage text, NO "
        "readable words anywhere. Text will be added later in post-processing.\n"
        "TOP-LEFT cell photo: [new article-relevant scene, no text].\n"
        "TOP-RIGHT cell photo: [new article-relevant scene, no text].\n"
        "BOTTOM-LEFT cell photo: [new article-relevant scene, no text].\n"
        "BOTTOM-RIGHT cell photo: [new article-relevant scene, no text].\n"
        "The 4 cells are equal and tile perfectly; no single full-image photo; no stacking; "
        "ZERO text anywhere in any cell; no real brands."
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
    skip_text: bool = False,
    cta_override: str = "",
) -> tuple[str, float]:
    """gpt-5.4-mini builds the 2x2 collage prompt. Returns ``(prompt, cost_usd)``.

    Default mode (``skip_text=False``): mandates the 3-band layout (white
    headline band on top, photo middle, white CTA band on bottom) on every
    cell regardless of seed style. The kie output IS the finished marketing
    card.

    ``skip_text=True`` mode: asks kie for CLEAN photographic cells with NO
    headline / CTA / overlay text. Used by the simple_x4 card-template path,
    which draws its own headline + CTA via Pillow afterwards. Plan
    ``_plans/2026-06-08-simple-x4-template-cards.md`` §D.2 (R1).

    ``cta_override`` (default mode only — ignored when ``skip_text=True``):
    when non-empty, the prompt instructs kie to use this EXACT text on every
    cell's bottom CTA band, overriding the default per-language "Read More"
    fallback. Used by the simple_x4 default path so operator-typed CTA cells
    drive the rendered CTA. Yoav 2026-06-08.

    ``article_excerpt`` (when given) grounds the new photo in the article
    topic rather than whatever the inspiration photo happened to show.

    When ``safety.matched`` the admin's sensitive-apparel safety block is
    appended to the user message before the LLM call, so the generated image
    description forces product-only frames with no humans.
    """
    if skip_text:
        user_message = _collage_user_message_no_text(description, article_excerpt)
    else:
        # cta_override gets baked INTO the body of the prompt (single source
        # of CTA truth) rather than appended as a separate "override" block.
        # The append-style block had kie rendering both the override AND the
        # per-language Read-More fallback stacked — see Yoav 2026-06-08
        # "Read More + Tenta 20 both showing".
        user_message = _collage_user_message(
            description, article_excerpt, cta_override=cta_override
        )
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
        skip_text=skip_text,
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
