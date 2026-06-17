"""Pillow CTA-overlay renderer for the cartoon tab.

Produces a transparent PNG the same size as the final cartoon video, with a
full-width yellow CTA pill anchored to the bottom of the canvas. The PNG is
fed to a Rendi ffmpeg ``overlay=0:0`` composite step so the pill appears at
the bottom of every cartoon video that has ``CTA = Yes`` set on the sheet.

Design (Yoav 2026-06-08: "a beautiful consistent CTA button design that
will match each size"):

  * Pill: full-width (94% of canvas), rounded ends, lifted clear of
    TikTok's bottom safe zone (the caption / username / sound ticker /
    progress-bar band that covers the lower part of the frame in-feed).
  * Color: bright yellow ``#FFC31E`` with bold black text — matches
    Template 2's CTA pill so the simple_x4 and cartoon outputs feel like
    one product family.
  * Typography: bundled Inter Variable Bold (full Latin Ext + Cyrillic +
    Greek + Vietnamese coverage). Font size auto-shrinks if the localised
    CTA text would overflow the pill width.
  * Scales by proportion (pill height = 8% of canvas, bottom margin = 19%
    to sit above TikTok's bottom UI + paid-ad CTA), so 1080×1920 (9:16),
    1080×1080 (1:1), and 1080×1350 (4:5) all render a visually consistent
    CTA without per-ratio tuning.

The PNG is mostly transparent; ffmpeg overlays it directly onto the cartoon
video frame at (0, 0). The transparent area passes through the video below.

Plan: ``_plans/2026-06-08-simple-x4-template-cards.md`` (cartoon CTA is a
follow-up to the simple_x4 CTA work; reuses the same per-language fallback).
"""

from __future__ import annotations

import io
from typing import Final

from PIL import Image, ImageDraw

from bulkvid.logging import get_logger
from bulkvid.pipeline.card_renderer import _draw_pill, _fit_pill_font, _load_font

_log = get_logger("cartoon_cta")


# ── Design tokens (match Template 2 for visual consistency) ──────────────────


PILL_BG: Final[tuple[int, int, int, int]] = (255, 195, 30, 255)       # yellow
PILL_TEXT_COLOR: Final[tuple[int, int, int]] = (20, 20, 20)            # near-black

# Layout proportions — relative to canvas height/width so every supported
# aspect ratio (9:16, 1:1, 4:5, 16:9) renders the same visual weight.
PILL_HEIGHT_FRAC: Final[float] = 0.08          # ~8% of canvas height
# Bottom margin as a fraction of canvas height. Lifted from the old 0.03
# (~58px on a 1920-tall frame, which sat the pill at y≈1708-1862 — buried
# under TikTok's UI) into TikTok's safe zone. TikTok overlays the bottom
# ~320px of a 1080×1920 frame (caption, username, sound ticker, progress
# bar; ad placements block ~370px for the native CTA), so content should
# stay above y≈1600. 0.19 puts the pill's BOTTOM edge at ~y1555 (≈81%
# down), clearing that band AND the ~370px paid-ad zone. Applies
# proportionally to every aspect ratio. (Yoav 2026-06-17: "the cta button …
# on tiktok it won't be visible, should be a bit upper to match tiktok".)
PILL_BOTTOM_MARGIN_FRAC: Final[float] = 0.19   # gap from canvas bottom (TikTok safe zone)
PILL_WIDTH_FRAC: Final[float] = 0.94           # near-full canvas width
PILL_FONT_HEIGHT_FRAC: Final[float] = 0.45     # font ≈ 45% of pill height
PILL_MIN_FONT_HEIGHT_FRAC: Final[float] = 0.25 # auto-shrink floor


def render_cartoon_cta_overlay_bytes(
    cta_text: str,
    *,
    canvas_width: int,
    canvas_height: int,
    font_override: str | None = None,
) -> bytes:
    """Render a transparent-PNG overlay carrying a single CTA pill at the bottom.

    Returns PNG bytes ready to upload to storage and feed into Rendi's
    ``overlay`` ffmpeg filter at position ``(0, 0)``. The transparent area
    is fully alpha=0 so the cartoon video below shows through everywhere
    except the pill region.

    ``cta_text`` must be non-empty — empty CTAs are filtered by the caller
    (the row processor only invokes this when ``cta_enabled`` is True).
    """
    if not cta_text or not cta_text.strip():
        raise ValueError("cartoon CTA overlay requires non-empty cta_text")
    if canvas_width <= 0 or canvas_height <= 0:
        raise ValueError(
            f"canvas dimensions must be positive (got {canvas_width}x{canvas_height})"
        )

    canvas = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # Pill geometry — proportional to the canvas so it looks consistent
    # across aspect ratios.
    pill_h = max(40, int(round(canvas_height * PILL_HEIGHT_FRAC)))
    pill_w = int(round(canvas_width * PILL_WIDTH_FRAC))
    pill_pad_x = int(round(canvas_width * 0.04))
    pill_pad_y = int(round(pill_h * 0.18))
    bottom_margin = int(round(canvas_height * PILL_BOTTOM_MARGIN_FRAC))
    pill_center_y = canvas_height - bottom_margin - pill_h // 2

    # Font size proportional to pill height + auto-shrink so long localised
    # CTAs (e.g. Polish "Dowiedz Się Więcej >>") still fit the pill.
    initial_font = max(20, int(round(pill_h * PILL_FONT_HEIGHT_FRAC)))
    min_font = max(14, int(round(pill_h * PILL_MIN_FONT_HEIGHT_FRAC)))
    pill_font = _fit_pill_font(
        draw, cta_text.strip(),
        max_width=pill_w - pill_pad_x * 2,
        initial_size=initial_font,
        min_size=min_font,
        font_override=font_override,
    )

    _draw_pill(
        canvas,
        cta_text.strip(),
        font=pill_font,
        bg=PILL_BG[:3],
        text_color=PILL_TEXT_COLOR,
        center_x=canvas_width // 2,
        center_y=pill_center_y,
        pad_x=pill_pad_x,
        pad_y=pill_pad_y,
        max_width=pill_w,
        min_width=pill_w,    # force full width — consistent look
    )

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    data = out.getvalue()
    _log.info(
        "cartoon_cta_overlay_rendered",
        cta_chars=len(cta_text),
        canvas=f"{canvas_width}x{canvas_height}",
        pill_h=pill_h,
        bytes_out=len(data),
    )
    return data
