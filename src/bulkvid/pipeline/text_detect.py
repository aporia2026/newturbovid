"""Vision check for "does this creative already carry a headline?".

The ``paste text on img`` tab overlays operator-typed text onto the Manual
Image. On big batches the team sometimes pastes creatives that are already
finished — the headline is burned into the pixels — and overlaying again
produces doubled text. This module asks gpt-5.4-mini to look at the image and
say whether a deliberate typeset overlay is already there, so the row
processor can pass those images through untouched.

Plan: ``_plans/2026-07-22-text-on-img-skip-already-texted-images.md``.

The whole design is built around one asymmetry:

  * calling "has text" on an image that has none  -> the ad ships with NO
    headline. Silent, and nobody notices until it is live.
  * calling "no text" on an image that has some   -> visibly doubled text on
    one row. Obvious on review, one re-run to fix.

So every ambiguous case must resolve toward "no text, go compose". Three
guards enforce that bias: the prompt lists scene-text categories as explicit
non-matches, the model must answer ``confidence: "high"`` for a skip to
count, and every failure path (exception, bad JSON, missing/odd fields)
returns :data:`NO_OVERLAY_TEXT` rather than raising. Same fail-open contract
as ``template_selector.select_default_template``: this check must never block
a row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from bulkvid.adapters.openai_client import MODEL_TEXT_DETECT, OpenAIClient
from bulkvid.logging import get_logger

_log = get_logger("text_detect")


# ── Result type ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OverlayTextVerdict:
    """One image's verdict.

    ``has_overlay_text`` is already gated on confidence — it is ``True`` only
    when the model said yes *and* said it was sure. Callers treat it as the
    final answer and do not re-apply the threshold.

    ``reason`` is free text straight from the model. It is only ever logged
    and stored in row metadata (truncated); it never reaches a prompt, a
    filename, or a URL.
    """

    has_overlay_text: bool
    confidence: str
    reason: str
    cost_usd: float


# Returned by every failure path, and by a genuine "clean image" answer.
NO_OVERLAY_TEXT = OverlayTextVerdict(
    has_overlay_text=False, confidence="", reason="", cost_usd=0.0
)


# ── Prompt ──────────────────────────────────────────────────────────────────

# The distinction this prompt has to make is "text a designer pasted on top"
# vs "text that was in front of the camera". The verticals this tab runs
# (real estate, dental, stair lifts, car deals) are full of the second kind:
# shop signage, plates, price tags, packaging. Treating those as a finished
# headline is the expensive mistake, so the negative cases are enumerated at
# least as carefully as the positive ones.
_DETECT_PROMPT = (
    "You are inspecting an advertising image. Answer ONE question: does this "
    "image ALREADY have a marketing headline or caption graphically overlaid "
    "on top of the picture?\n\n"
    "COUNTS AS YES — text added on top of the photo in post-production:\n"
    "- A large headline or slogan laid over the picture.\n"
    "- A caption bar, coloured banner, or white band containing words.\n"
    "- A call-to-action button or badge with words on it (\"Learn More\", "
    "\"Read More\", a price or discount badge added by a designer).\n"
    "- Meme-style or sticker-style text, or subtitle text burned in.\n\n"
    "COUNTS AS NO — words that were physically in the scene when the photo "
    "was taken, or that are not a headline:\n"
    "- Shop signs, street signs, billboards, building names, house numbers.\n"
    "- Licence plates, vehicle liveries, road markings.\n"
    "- Product packaging, labels, price tags, menus, books, documents, "
    "paperwork, forms.\n"
    "- Text on a screen, phone, monitor, or television inside the photo.\n"
    "- Printing on clothing, uniforms, or equipment.\n"
    "- A small logo, watermark, photographer credit, or stock-photo mark in "
    "a corner.\n"
    "- No readable words at all.\n\n"
    "Decide by INTENT and PRESENTATION, not by whether words are readable. "
    "If the words look like they belong to the photographed scene rather "
    "than to a designer's layer, answer no. If you are not sure, answer no "
    "and set confidence to \"low\".\n\n"
    "Reply with STRICT JSON and nothing else, exactly these three keys:\n"
    "{\"has_overlay_text\": true|false, \"confidence\": \"high\"|\"low\", "
    "\"reason\": \"one short sentence\"}\n"
    "Use confidence \"high\" only when the image is unambiguous."
)


# ── Detector ────────────────────────────────────────────────────────────────


async def detect_overlay_text(
    client: OpenAIClient,
    image_b64: str,
    *,
    model: str = MODEL_TEXT_DETECT,
) -> OverlayTextVerdict:
    """Ask the vision model whether ``image_b64`` already carries a headline.

    The image is passed as base64 rather than by URL on purpose. OpenAI would
    otherwise fetch the source itself, and Facebook's ad endpoint drops any
    client whose TLS fingerprint is not a real browser — which is exactly
    where most of this tab's Manual Images come from. See
    ``_plans/2026-06-30-facebook-tls-fingerprint.md``.

    Never raises. Returns :data:`NO_OVERLAY_TEXT` on any anomaly, which the
    caller reads as "not sure — compose the overlay as usual".
    """
    try:
        result = await client.vision_describe(
            prompt=_DETECT_PROMPT,
            image_b64=image_b64,
            model=model,
            detail="high",
            max_tokens=200,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except Exception as e:    # a detector failure must NEVER block the row
        _log.warning(
            "overlay_text_detect_call_failed",
            error=type(e).__name__,
            detail=str(e)[:200],
        )
        return NO_OVERLAY_TEXT

    try:
        parsed: dict[str, Any] = json.loads(result.text)
    except json.JSONDecodeError as e:
        _log.warning(
            "overlay_text_detect_parse_failed",
            error=str(e),
            raw_preview=result.text[:200],
        )
        return _clean(result.cost_usd)
    if not isinstance(parsed, dict):
        _log.warning(
            "overlay_text_detect_parse_failed",
            error=f"top-level value is {type(parsed).__name__}, expected object",
            raw_preview=result.text[:200],
        )
        return _clean(result.cost_usd)

    raw_flag = parsed.get("has_overlay_text")
    # Strict bool only. A model that answers "yes" / 1 / "true" is off-script
    # enough that we'd rather compose than trust it into a silent skip.
    if not isinstance(raw_flag, bool):
        _log.warning(
            "overlay_text_detect_bad_flag",
            returned=repr(raw_flag)[:80],
            raw_preview=result.text[:200],
        )
        return _clean(result.cost_usd)

    confidence = str(parsed.get("confidence") or "").strip().lower()
    reason = str(parsed.get("reason") or "").strip()[:200]

    # Confidence gate: only an unambiguous yes earns a skip. "low", a missing
    # value, or anything unrecognised falls through to the compose path.
    if raw_flag and confidence != "high":
        _log.info(
            "overlay_text_detect_low_confidence",
            confidence=confidence or "(missing)",
            reason=reason,
            cost_usd=result.cost_usd,
        )
        return OverlayTextVerdict(
            has_overlay_text=False,
            confidence=confidence,
            reason=reason,
            cost_usd=result.cost_usd,
        )

    _log.info(
        "overlay_text_detect_ok",
        has_overlay_text=raw_flag,
        confidence=confidence or "(missing)",
        reason=reason,
        cost_usd=result.cost_usd,
    )
    return OverlayTextVerdict(
        has_overlay_text=raw_flag,
        confidence=confidence,
        reason=reason,
        cost_usd=result.cost_usd,
    )


def _clean(cost_usd: float) -> OverlayTextVerdict:
    """A "compose as usual" verdict that still carries the spend.

    Used when the call itself succeeded (so we were billed) but the answer was
    unusable. Keeping the cost means a batch's reported spend stays honest
    even when the detector is having a bad day.
    """
    return OverlayTextVerdict(
        has_overlay_text=False, confidence="", reason="", cost_usd=cost_usd
    )
