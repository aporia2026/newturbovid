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


def _collage_user_message(description: str) -> str:
    return (
        f"Inspiration ad analysis:\n{description}\n\n"
        "Create ONE image that is a 2x2 GRID of exactly 4 equal-sized panels:\n"
        "  TOP-LEFT = Panel 1 | TOP-RIGHT = Panel 2\n"
        "  BOTTOM-LEFT = Panel 3 | BOTTOM-RIGHT = Panel 4\n\n"
        "Each panel is a COMPLETE, standalone vertical ad frame inspired by the "
        "analysis above — same subject, style, palette and mood — showing the "
        "product/subject in a slightly different scene or angle so the 4 panels "
        "feel like a varied ad set.\n\n"
        "TEXT & CTA (important):\n"
        "- Render a SHORT marketing headline plus a clear call-to-action on EACH "
        "panel, in the SAME LANGUAGE as the inspiration's text.\n"
        "- Do NOT copy the inspiration's wording verbatim — write SIMILAR, natural "
        "marketing copy with the same intent and angle, varied across the 4 panels.\n"
        "- Use only generic, unbranded copy — NEVER put a real brand or company "
        "name in the headline or CTA.\n"
        "- Text must be crisp, correctly spelled, and legible: large headline, smaller CTA.\n\n"
        "NO REAL BRANDS (strict — legal requirement):\n"
        "- The panels must contain NO real brand logos, trademarks, brand names, "
        "badges, or recognisable branding — not even if the inspiration shows them.\n"
        "- Replace any branding from the inspiration with generic, unbranded "
        "equivalents; show the product as a generic, unbranded item.\n\n"
        "LAYOUT RULES (must follow):\n"
        "- ONE image, 2 columns x 2 rows, 4 equal panels, thin neutral divider between them.\n"
        "- Do NOT stack panels into a single column. Do NOT repeat the same panel.\n\n"
        "FORMAT your response exactly like this and nothing else:\n"
        "Create a 2x2 grid collage (2 columns, 2 rows, 4 equal panels), each a vertical ad frame.\n"
        'TOP-LEFT panel: [scene] with headline "[short headline]" and CTA "[short cta]".\n'
        'TOP-RIGHT panel: [scene] with headline "[short headline]" and CTA "[short cta]".\n'
        'BOTTOM-LEFT panel: [scene] with headline "[short headline]" and CTA "[short cta]".\n'
        'BOTTOM-RIGHT panel: [scene] with headline "[short headline]" and CTA "[short cta]".\n'
        "All panels: same style and quality as the inspiration; text legible and correctly spelled."
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
) -> tuple[str, float]:
    """gpt-5.4-mini builds the 2x2 collage prompt. Returns ``(prompt, cost_usd)``."""
    _log.info("collage_prompt_submit", description_chars=len(description))
    result = await client.chat(
        model=model,
        messages=[
            {"role": "system", "content": _COLLAGE_SYSTEM},
            {"role": "user", "content": _collage_user_message(description)},
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
