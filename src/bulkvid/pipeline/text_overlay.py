"""Pillow text-on-image overlay for the ``paste text on img`` tab.

Renders heavy white text with a thick black outline, centered on both axes
of the FINAL canvas (the row's target aspect ratio), with the operator's
source image composited inside via a blurred-background fit. Text is
auto-wrapped to a side margin and auto-shrunk to fit a max number of
lines — at FINAL canvas dimensions, so the visual text size is
consistent across every row regardless of the source's aspect ratio.

Pipeline integration: the row processor downloads the manual image,
the renderer composites it into a target-aspect canvas with a blurred
copy of itself behind, paints the text overlay on top, and uploads the
result. Rendi then animates that pre-composed image; its
image_to_video_fit becomes a no-op since the image is already at the
target dimensions.

History:
  * v1 cover-cropped the source to target — destroyed framed-in
    composition (1:1 stock photo with "Remote NHS Receptionists" baked
    across the top got chopped in half when forced into 9:16).
  * v2 rendered text at the source's native dimensions and let Rendi
    fit afterwards — preserved the source but the visual text size
    varied wildly between rows (landscape sources got tiny text after
    being letterboxed; portrait sources got big text). Chat 2026-06-09.
  * v3 does the blurred-background fit in Python at target dimensions
    and renders text on the final canvas. Source preserved AND text
    size consistent.
  * v4 (current) fits adaptively: near-target sources are cover-cropped
    to fill the canvas edge-to-edge (no blurred bars), mismatched ones
    keep the v3 blur fit. Chat 2026-06-10 — operator flagged the bars
    on a near-4:3 source rendered at 4:3.

Plan: ``_plans/2026-06-09-paste-text-on-img-tab.md``.
"""

from __future__ import annotations

import io
from typing import Final

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from bulkvid.adapters.rendi import dimensions_for_ratio
from bulkvid.logging import get_logger
from bulkvid.pipeline.card_renderer import (
    _fit_image_cover,
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
        font = _load_font(size, override=None, text=text)
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
    font = _load_font(min_size, override=None, text=text)
    return font, _wrap_text_to_width(draw, text, font, max_width)


# Adaptive fit threshold: when filling the canvas (cover-crop) would lose
# at most this fraction of one source dimension, crop — a thin trimmed edge
# beats visible blurred bars (chat 2026-06-10: "adjust to the sizes, but
# only if it makes sense, not on every 9:16"). Beyond it, keep the
# blurred-background fit so a landscape source forced into 9:16 isn't
# destroyed by a 60%+ crop. Applies to text_on_img ONLY — the simple /
# 4images video tabs keep their never-crop fit because their manual images
# are finished ad creatives with text baked in.
_MAX_COVER_CROP_LOSS: Final[float] = 0.20


def _fit_mode(src_w: int, src_h: int, target_w: int, target_h: int) -> str:
    """Decide how ``src`` should fill the target canvas.

    ``'cover'`` when the aspect mismatch is small enough that cropping to
    fill loses at most ``_MAX_COVER_CROP_LOSS`` of one dimension;
    ``'blur'`` when the mismatch is bigger and cropping would gut the
    composition.
    """
    ratio = (src_w / src_h) / (target_w / target_h)
    loss = 1.0 - min(ratio, 1.0 / ratio)
    return "cover" if loss <= _MAX_COVER_CROP_LOSS else "blur"


def _blurred_bg_fit(
    src: Image.Image, target_w: int, target_h: int
) -> Image.Image:
    """Fit ``src`` into a ``target_w × target_h`` canvas, adaptively.

    Near-target sources (per ``_fit_mode``) are cover-cropped to fill the
    whole canvas — no bars at all, at the cost of a thin trimmed edge.
    Mismatched sources (e.g. landscape into 9:16) keep the v3 behavior:
    the source is preserved uncropped, centered over a blurred,
    cover-cropped copy of itself, with blurred bars on the short sides.
    """
    src_w, src_h = src.size

    mode = _fit_mode(src_w, src_h, target_w, target_h)
    _log.info(
        "image_fit",
        mode=mode,
        src_size=f"{src_w}x{src_h}",
        target_size=f"{target_w}x{target_h}",
    )
    if mode == "cover":
        return _fit_image_cover(src, target_w, target_h)

    # Background: cover-crop to fill canvas, then heavy blur. The radius
    # scales with target size so 720p and 1080p both look smooth.
    bg = _fit_image_cover(src, target_w, target_h)
    try:
        bg = bg.filter(ImageFilter.GaussianBlur(radius=max(20, target_h // 60)))
    except Exception:
        pass

    # Foreground: fit-inside scale (preserve aspect, no crop).
    scale = min(target_w / src_w, target_h / src_h)
    fg_w = max(1, int(round(src_w * scale)))
    fg_h = max(1, int(round(src_h * scale)))
    fg = src.resize((fg_w, fg_h), Image.Resampling.LANCZOS)

    # Composite fg centered on bg.
    try:
        fg_x = (target_w - fg_w) // 2
        fg_y = (target_h - fg_h) // 2
        bg.paste(fg, (fg_x, fg_y))
    finally:
        fg.close()
    return bg


def overlay_text_on_image_bytes(
    image_bytes: bytes,
    text: str,
    *,
    aspect_ratio: str = "9:16",
    output_format: str = "PNG",
) -> bytes:
    """Composite ``image_bytes`` into a target-aspect canvas with a
    blurred-background fit, then center heavy white-with-black-outline
    text on the FINAL canvas. Returns image bytes (PNG by default)
    ready for storage upload.

    Text geometry is computed against the target canvas, NOT the source
    image, so the visual text size is consistent across every row
    regardless of source aspect (the v2 bug — landscape sources got
    tiny text after Rendi letterboxed them; portrait sources got big
    text).

    Empty text is allowed — returns the blurred-bg-fit image with no
    overlay drawn, so a row with a typo'd or accidentally-blank Text
    cell still ships a valid video.
    """
    width, height = dimensions_for_ratio(aspect_ratio)

    with Image.open(io.BytesIO(image_bytes)) as src:
        src.load()
        src_rgb = src.convert("RGB")

    try:
        canvas = _blurred_bg_fit(src_rgb, width, height)
    finally:
        src_rgb.close()

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
