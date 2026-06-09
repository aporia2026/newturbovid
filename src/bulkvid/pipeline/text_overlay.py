"""Pillow text-on-image overlay for the ``paste text on img`` tab.

Renders heavy white text with a thick black outline, centered on both axes
of the operator-supplied image. The text auto-wraps to fit a comfortable
side margin and auto-shrinks to fit a max number of lines.

Pipeline integration: the row processor downloads the manual image,
paints the overlay AT THE SOURCE'S NATIVE DIMENSIONS (no crop, no resize),
and uploads the result. Rendi then does its blurred-background fit to
the row's target aspect ratio — preserving the operator's original
composition instead of chopping framed-in headlines off the sides.

Earlier the renderer cover-cropped the source to ``aspect_ratio`` first;
that destroyed images that already had text/composition designed for
1:1 or another aspect (e.g. a 1:1 stock photo with "Remote NHS
Receptionists" baked across the top got chopped in half when forced
into 9:16). See chat 2026-06-09.

Plan: ``_plans/2026-06-09-paste-text-on-img-tab.md``.
"""

from __future__ import annotations

import io
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from bulkvid.logging import get_logger
from bulkvid.pipeline.card_renderer import (
    _load_font,
    _wrap_text_to_width,
)

_log = get_logger("text_overlay")


# Visual constants — match the user's reference mockup (chat 2026-06-09).
# Fill is white, stroke is solid black; the stroke ratio is calibrated to
# read clearly on bright, dark, AND high-contrast backgrounds (a photo's
# overlay can land on any of those across a single video frame).
_FILL: Final[tuple[int, int, int]] = (255, 255, 255)
_STROKE: Final[tuple[int, int, int]] = (0, 0, 0)

# Text block geometry as fractions of canvas dimensions.
_SIDE_PADDING_FRAC: Final[float] = 0.05    # 5% margin per side
_MAX_LINES: Final[int] = 4                 # auto-wrap stops here, then shrinks

# Font size walks DOWN from `_INITIAL_FRAC` until the wrapped block fits
# inside the safe vertical band (top/bottom 10% reserved) AND every line
# fits inside the horizontal safe zone; floors at `_MIN_FRAC` accepting
# overflow rather than degrading further. The initial fraction is sized
# so a 4-word headline at 9:16 lands comfortably; longer headlines
# auto-shrink.
_INITIAL_FRAC: Final[float] = 0.09
_MIN_FRAC: Final[float] = 0.04

# Stroke thickness as a fraction of font size. ~7% gives a chunky outline
# at any size — matches the reference mockup's heavy "highlight" feel.
_STROKE_WIDTH_FRAC: Final[float] = 0.07


def _fit_overlay_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    max_width: int,
    max_height: int,
    initial_size: int,
    min_size: int,
) -> tuple[ImageFont.ImageFont, list[str]]:
    """Pick the largest font where the wrapped block fits inside the
    safe (max_width × max_height) box AND no individual line exceeds
    ``max_width``. Returns the font and the wrapped lines so the caller
    draws without re-wrapping.

    The width-per-line check matters because ``_wrap_text_to_width``
    always places at least one word on a line — a single long word
    (e.g. "oportunidades" at a huge initial size) wraps to its own line
    but still overflows. Without this check the auto-fit thinks the
    block fits and ships text running off the canvas.
    """
    size = initial_size
    while size >= min_size:
        font = _load_font(size, override=None)
        lines = _wrap_text_to_width(draw, text, font, max_width)
        if not lines:
            return font, []
        if len(lines) <= _MAX_LINES:
            # Width check: every wrapped line must fit horizontally.
            widest = max(
                draw.textbbox((0, 0), ln, font=font)[2]
                - draw.textbbox((0, 0), ln, font=font)[0]
                for ln in lines
            )
            line_heights = [
                draw.textbbox((0, 0), ln or " ", font=font)[3]
                - draw.textbbox((0, 0), ln or " ", font=font)[1]
                for ln in lines
            ]
            line_spacing = int(size * 0.18)
            total_h = sum(line_heights) + line_spacing * (len(lines) - 1)
            if widest <= max_width and total_h <= max_height:
                return font, lines
        size -= max(2, size // 20)
    # Floor: accept overflow at min_size rather than degrade further.
    font = _load_font(min_size, override=None)
    return font, _wrap_text_to_width(draw, text, font, max_width)


def overlay_text_on_image_bytes(
    image_bytes: bytes,
    text: str,
    *,
    aspect_ratio: str = "9:16",
    output_format: str = "PNG",
) -> bytes:
    """Overlay heavy white-with-black-outline text centered on
    ``image_bytes`` AT THE SOURCE'S NATIVE DIMENSIONS. Returns image
    bytes (PNG by default) ready for storage upload — Rendi handles the
    aspect-ratio fit downstream.

    ``aspect_ratio`` is kept on the signature for API compatibility and
    future tuning, but the renderer no longer resizes/crops the source.
    Forcing a 1:1 photo into 9:16 (the previous behavior) chopped any
    edge composition the operator carefully framed.

    Empty text is allowed — returns the unmodified source so a row with
    a typo'd or accidentally-blank Text cell still ships a valid video.
    """
    del aspect_ratio    # kept for API compat; intentionally unused

    with Image.open(io.BytesIO(image_bytes)) as src:
        src.load()
        canvas = src.convert("RGB")

    width, height = canvas.size

    text = (text or "").strip()
    if not text:
        _log.info(
            "text_overlay_skip_blank",
            width=width,
            height=height,
        )
        buf = io.BytesIO()
        canvas.save(buf, format=output_format, optimize=True)
        canvas.close()
        return buf.getvalue()

    draw = ImageDraw.Draw(canvas)
    side_padding = int(width * _SIDE_PADDING_FRAC)
    max_w = width - side_padding * 2
    # Reserve top + bottom safe zones so the overlay doesn't crowd the
    # frame edges (also leaves space for ZapCap if the row turns it on).
    safe_top = int(height * 0.10)
    safe_bottom = int(height * 0.10)
    max_h = height - safe_top - safe_bottom

    font, lines = _fit_overlay_font(
        draw,
        text,
        max_width=max_w,
        max_height=max_h,
        initial_size=int(height * _INITIAL_FRAC),
        min_size=max(20, int(height * _MIN_FRAC)),
    )

    if not lines:
        _log.warning(
            "text_overlay_no_lines",
            text_chars=len(text),
            width=width,
            height=height,
        )
        buf = io.BytesIO()
        canvas.save(buf, format=output_format, optimize=True)
        canvas.close()
        return buf.getvalue()

    # Stroke thickness scales with the actual font size we landed on, so
    # the outline reads correctly whether the auto-fit kept the initial
    # ~13% or shrunk down to the ~5% floor.
    font_size_actual = getattr(font, "size", int(height * _INITIAL_FRAC))
    stroke_width = max(2, int(round(font_size_actual * _STROKE_WIDTH_FRAC)))

    line_heights = [
        draw.textbbox((0, 0), ln or " ", font=font)[3]
        - draw.textbbox((0, 0), ln or " ", font=font)[1]
        for ln in lines
    ]
    line_spacing = int(font_size_actual * 0.18)
    block_h = sum(line_heights) + line_spacing * (len(lines) - 1)
    # Center the block vertically on the canvas (not on the safe zone) so
    # the visual centerline of the text matches the visual centerline of
    # the photo — matches the reference mockup.
    y = (height - block_h) // 2

    for ln, lh in zip(lines, line_heights, strict=True):
        bbox = draw.textbbox((0, 0), ln, font=font)
        tw = bbox[2] - bbox[0]
        tx = (width - tw) // 2 - bbox[0]
        ty = y - bbox[1]
        draw.text(
            (tx, ty),
            ln,
            fill=_FILL,
            font=font,
            stroke_width=stroke_width,
            stroke_fill=_STROKE,
        )
        y += lh + line_spacing

    _log.info(
        "text_overlay_rendered",
        width=width,
        height=height,
        text_chars=len(text),
        line_count=len(lines),
        font_size=font_size_actual,
        stroke_width=stroke_width,
    )

    buf = io.BytesIO()
    canvas.save(buf, format=output_format, optimize=True)
    canvas.close()
    return buf.getvalue()
