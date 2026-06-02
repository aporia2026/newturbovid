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


# Heuristic for the aerial-view rule from CBImageNoText (hides car brand badges).
_AUTOMOTIVE_KEYWORDS = (
    "car", "vehicle", "automobile", "sedan", "suv", "truck", "dealership",
    "parking", "automotive", "bmw", "mercedes", "toyota", "honda", "ford",
    "audi", "volkswagen", "hyundai", "kia", "nissan", "chevrolet", "tesla",
)


_DESCRIBE_PROMPT = (
    "Analyse this advertising image as a creative director. "
    "Ignore ALL text, logos, and branding completely. Describe only the visual content.\n\n"
    "Cover these points concisely:\n"
    "1. SUBJECT: What is the main subject or product?\n"
    "2. SETTING: Where is the scene taking place?\n"
    "3. STYLE: Is it photographic, illustrated, cartoon, realistic, etc?\n"
    "4. COLORS: What is the dominant color palette and mood?\n"
    "5. COMPOSITION: How is the scene framed?\n"
    "6. STORY POTENTIAL: In one sentence, what visual story could 4 panels tell about this subject?\n\n"
    "Return only the structured description, no preamble, no commentary."
)


_COLLAGE_SYSTEM = (
    "You are a world-class advertising creative director specializing in "
    "image-editing prompts. You write ultra-precise prompts for "
    "google/nano-banana-edit that produce perfect 2x2 grid collages. "
    "Your prompts are always obeyed exactly because you are extremely "
    "specific about layout and content."
)


def _automotive_rule_for(description: str) -> str:
    lower = description.lower()
    if not any(word in lower for word in _AUTOMOTIVE_KEYWORDS):
        return ""
    return (
        "AUTOMOTIVE RULE: Since this image contains vehicles, use aerial/"
        "bird's-eye view or elevated overhead angles for all panels. This "
        "naturally hides brand badges, grilles, and logos that appear on the "
        "front/rear of cars. Show cars from above or at a high angle looking "
        "down — like drone photography.\n"
    )


def _collage_user_message(description: str) -> str:
    return (
        f"Image description:\n{description}\n\n"
        "Write a prompt for google/nano-banana-edit. "
        "The output must be a SINGLE IMAGE that is a 2x2 GRID of exactly 4 equal-sized panels arranged as follows:\n"
        "  TOP-LEFT = Panel 1 | TOP-RIGHT = Panel 2\n"
        "  BOTTOM-LEFT = Panel 3 | BOTTOM-RIGHT = Panel 4\n\n"
        "The 4 panels tell a short visual story based on the image description above. "
        "Each panel is a distinct scene or moment in a natural progression "
        "(e.g. wide establishing shot → closer view → key action moment → outcome/resolution).\n\n"
        "CRITICAL LAYOUT RULES — the model MUST follow these:\n"
        "- The result is ONE image divided into a 2-column × 2-row grid.\n"
        "- All 4 panels are exactly the same width and height.\n"
        "- There is a thin neutral dividing line between panels.\n"
        "- Do NOT stack all panels vertically. Do NOT make a single-column layout.\n"
        "- Do NOT repeat the same image across panels.\n\n"
        f"{_automotive_rule_for(description)}"
        "BRAND & LOGO RULES — strictly enforced:\n"
        "- NO car brand logos, badges, emblems, or grille designs (no BMW, Mercedes, Toyota, etc.).\n"
        "- NO brand names on any vehicle, product, or surface.\n"
        "- If showing vehicles, use generic unnamed car shapes with no identifying brand marks.\n"
        "- NO license plates, NO dealership signs, NO branded clothing or accessories.\n\n"
        "CONTENT RULES for every panel:\n"
        "- Absolutely NO text, NO words, NO letters, NO numbers, NO logos, NO watermarks, NO labels.\n"
        "- Same visual style, color palette, and lighting quality across all 4 panels.\n"
        "- Each panel must look like a professional advertising photo or illustration.\n\n"
        "FORMAT your response exactly like this and nothing else:\n"
        "Create a 2x2 grid collage (2 columns, 2 rows, 4 equal panels).\n"
        "TOP-LEFT panel: [vivid scene description for panel 1].\n"
        "TOP-RIGHT panel: [vivid scene description for panel 2].\n"
        "BOTTOM-LEFT panel: [vivid scene description for panel 3].\n"
        "BOTTOM-RIGHT panel: [vivid scene description for panel 4].\n"
        "All panels: same style, same quality, NO text, NO logos, NO brand marks anywhere."
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
