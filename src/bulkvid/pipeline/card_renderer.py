"""Pillow-based card renderer for the ``simple x4`` template feature.

Two templates ship in the initial cut (mockups under
``apps_script/template_previews/``):

  - Template 1 (blue/purple): image fills the top, white strip at the bottom
    with a bold purple headline centered and a pink rounded CTA pill below.
  - Template 2 (green gradient): image fills the canvas, a vertical
    green-to-dark-green gradient overlays the lower portion to keep the
    headline readable; bold white headline + yellow rounded CTA pill.

Plan: ``_plans/2026-06-08-simple-x4-template-cards.md`` §D.2 (R1 chosen),
§D.6 (renderer scales to any aspect ratio).

Font resolution
---------------
Inter Bold ships in the repo (``src/bulkvid/assets/fonts/Inter-Bold.ttf``,
SIL OFL). It's the closest free font to the user-supplied mockups (modern
geometric bold sans with full Latin Extended for accents like "é") and gives
deterministic output across every deploy target. The system-font fallback
chain stays as a safety net in case the bundled file is ever missing.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from bulkvid.logging import get_logger

_log = get_logger("card_render")


# ── Public types ─────────────────────────────────────────────────────────────


TEMPLATE_1: Final[str] = "1"
TEMPLATE_2: Final[str] = "2"
SUPPORTED_TEMPLATES: Final[frozenset[str]] = frozenset({TEMPLATE_1, TEMPLATE_2})


@dataclass(frozen=True)
class CardDesign:
    """One template's visual identity. New templates = new instance + dispatch."""

    template_id: str
    # Bottom strip height as a fraction of canvas height. Template 1 uses a
    # white strip below the image; template 2 uses a gradient overlay.
    strip_height_frac: float
    # Background image cover height (fraction of canvas). For template 1 the
    # image stops above the strip; for template 2 the image fills the whole
    # canvas and the gradient overlays the lower portion.
    image_cover_frac: float
    # Title text color (R,G,B).
    title_color: tuple[int, int, int]
    # CTA pill colors.
    cta_bg: tuple[int, int, int]
    cta_text_color: tuple[int, int, int]
    # Background color of the bottom strip (only for template 1; template 2
    # uses the gradient instead).
    strip_bg: tuple[int, int, int] | None
    # Thin accent line under the image (only template 1).
    accent_line_color: tuple[int, int, int] | None
    # Optional gradient start/end colors for template 2.
    gradient_top: tuple[int, int, int] | None
    gradient_bottom: tuple[int, int, int] | None


_DESIGNS: Final[dict[str, CardDesign]] = {
    TEMPLATE_1: CardDesign(
        template_id=TEMPLATE_1,
        strip_height_frac=0.22,
        image_cover_frac=0.78,
        title_color=(120, 60, 235),       # vivid purple from mockup
        cta_bg=(255, 50, 100),            # pink/red pill
        cta_text_color=(255, 255, 255),
        strip_bg=(255, 255, 255),
        accent_line_color=(255, 50, 100),
        gradient_top=None,
        gradient_bottom=None,
    ),
    TEMPLATE_2: CardDesign(
        template_id=TEMPLATE_2,
        strip_height_frac=0.32,
        image_cover_frac=1.00,            # image fills, gradient overlays
        title_color=(255, 255, 255),      # white over gradient
        cta_bg=(255, 195, 30),            # yellow pill
        cta_text_color=(20, 20, 20),
        strip_bg=None,
        accent_line_color=None,
        gradient_top=(40, 220, 50),       # bright green top
        gradient_bottom=(5, 60, 5),       # dark green/black bottom
    ),
}


# ── Font resolution ──────────────────────────────────────────────────────────


_BUNDLED_FONT_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Inter-Variable.ttf"
)

# Variable-font axes for Bold display weight. ``opsz`` (14) = display-optimized
# glyph shapes; ``wght`` (700) = Bold. Pillow's set_variation_by_axes() expects
# values in the axis order the font declares — for Inter that's [opsz, wght].
_BUNDLED_FONT_AXES: Final[tuple[float, float]] = (14.0, 700.0)

# Legacy static Bold variant (basic Latin only — missing Polish ł, ę etc.).
# Kept as a final fallback for the unlikely case the variable font is missing
# or its variation API isn't available; never picked when the variable font
# is present. Will be removed once the variable font has shipped for a while.
_LEGACY_BUNDLED_FONT_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Inter-Bold.ttf"
)

_SYSTEM_FONT_CANDIDATES: Final[tuple[str, ...]] = (
    # Linux deploy targets first (production matters more than dev).
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    # Windows dev box.
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    # macOS dev box.
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
)


def _find_font_path(override: str | None = None) -> str | None:
    """Return the first usable TTF path, or None to trigger the bitmap fallback.

    Resolution order: explicit override → bundled Inter Variable (full Latin
    Ext, Cyrillic, Greek, Vietnamese) → legacy bundled Inter Bold (basic
    Latin only) → system fonts.
    """
    if override and os.path.isfile(override):
        return override
    if _BUNDLED_FONT_PATH.is_file():
        return str(_BUNDLED_FONT_PATH)
    if _LEGACY_BUNDLED_FONT_PATH.is_file():
        return str(_LEGACY_BUNDLED_FONT_PATH)
    for candidate in _SYSTEM_FONT_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def _load_font(size: int, override: str | None = None) -> ImageFont.ImageFont:
    """Load the bundled font at ``size`` px in Bold (wght=700).

    The Inter variable font ships with the opsz+wght axes; we pin opsz=14
    (display-shape glyphs) and wght=700 (Bold) so every render call
    produces consistent display-bold text. Falls back to default-weight
    glyphs on non-variable fonts (no exception) and to PIL's bitmap font
    on missing files.
    """
    path = _find_font_path(override)
    if path is None:
        _log.warning("card_font_fallback_default", size=size)
        return ImageFont.load_default()
    try:
        font = ImageFont.truetype(path, size=size)
    except OSError as e:
        _log.warning("card_font_load_failed", path=path, error=str(e))
        return ImageFont.load_default()

    # Variable fonts need the weight axis pinned to render Bold; static
    # fonts (e.g. the legacy Inter-Bold.ttf fallback) raise OSError on
    # set_variation_by_axes — swallow it, the static font is already Bold.
    try:
        font.set_variation_by_axes(list(_BUNDLED_FONT_AXES))
    except (OSError, AttributeError):
        pass
    return font


# ── Layout helpers ───────────────────────────────────────────────────────────


def _fit_image_cover(src: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Cover-crop ``src`` to exactly ``target_w × target_h`` preserving aspect."""
    src_w, src_h = src.size
    src_aspect = src_w / src_h
    target_aspect = target_w / target_h

    if src_aspect > target_aspect:
        # Source is wider — fit height, crop sides.
        new_h = target_h
        new_w = int(round(new_h * src_aspect))
    else:
        new_w = target_w
        new_h = int(round(new_w / src_aspect))

    resized = src.resize((new_w, new_h), Image.Resampling.LANCZOS)
    try:
        left = (new_w - target_w) // 2
        top = (new_h - target_h) // 2
        return resized.crop((left, top, left + target_w, top + target_h))
    finally:
        if resized is not src:
            resized.close()


def _wrap_text_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    """Greedy word-wrap so each line fits within ``max_width`` px."""
    words = (text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width or not current:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def _fit_title_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_height: int,
    *,
    max_lines: int,
    initial_size: int,
    min_size: int,
    font_override: str | None,
) -> tuple[ImageFont.ImageFont, list[str]]:
    """Pick the largest font size where the wrapped text fits the box.

    Walks the font size DOWN from ``initial_size`` until the wrapped block
    fits both width and height constraints; floors at ``min_size``. Returns
    the font and the wrapped lines so callers can draw without re-wrapping.
    """
    size = initial_size
    while size >= min_size:
        font = _load_font(size, override=font_override)
        lines = _wrap_text_to_width(draw, text, font, max_width)
        if not lines:
            return font, []
        if len(lines) <= max_lines:
            # Measure total block height.
            line_heights = [
                draw.textbbox((0, 0), ln or " ", font=font)[3]
                - draw.textbbox((0, 0), ln or " ", font=font)[1]
                for ln in lines
            ]
            line_spacing = int(size * 0.18)
            total_h = sum(line_heights) + line_spacing * (len(lines) - 1)
            if total_h <= max_height:
                return font, lines
        size -= max(2, size // 20)
    # Floor: accept overflow at min_size rather than degrade further.
    font = _load_font(min_size, override=font_override)
    lines = _wrap_text_to_width(draw, text, font, max_width)
    return font, lines


def _vertical_gradient(
    width: int,
    height: int,
    top: tuple[int, int, int],
    bottom: tuple[int, int, int],
    *,
    top_alpha: int = 60,
    bottom_alpha: int = 240,
) -> Image.Image:
    """Build an RGBA gradient image (transparent top, opaque bottom).

    The alpha ramp keeps the upper portion of the canvas image visible while
    making the lower portion dark enough for white text to be legible.
    """
    grad = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = grad.load()
    if pixels is None:    # defensive; PIL always returns one
        return grad
    for y in range(height):
        t = y / max(1, height - 1)
        r = int(round(top[0] + (bottom[0] - top[0]) * t))
        g = int(round(top[1] + (bottom[1] - top[1]) * t))
        b = int(round(top[2] + (bottom[2] - top[2]) * t))
        a = int(round(top_alpha + (bottom_alpha - top_alpha) * t))
        for x in range(width):
            pixels[x, y] = (r, g, b, a)
    return grad


def _draw_pill(
    canvas: Image.Image,
    text: str,
    *,
    font: ImageFont.ImageFont,
    bg: tuple[int, int, int],
    text_color: tuple[int, int, int],
    center_x: int,
    center_y: int,
    pad_x: int,
    pad_y: int,
    max_width: int,
    min_width: int = 0,
) -> tuple[int, int]:
    """Draw a centered rounded-rectangle pill. Returns (pill_w, pill_h).

    ``min_width`` forces the pill to be at least that wide regardless of
    text length — used by Template 2 to render a full-width CTA pill that
    matches the user-supplied mockup. Default 0 = hugs the text + padding.
    """
    draw = ImageDraw.Draw(canvas)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    pill_w = min(max(text_w + pad_x * 2, min_width), max_width)
    pill_h = text_h + pad_y * 2
    left = center_x - pill_w // 2
    top = center_y - pill_h // 2
    right = left + pill_w
    bottom = top + pill_h
    radius = pill_h // 2
    draw.rounded_rectangle(
        (left, top, right, bottom), radius=radius, fill=bg
    )
    # Text baseline correction: textbbox y0 is non-zero for tall fonts.
    tx = center_x - text_w // 2 - bbox[0]
    ty = center_y - text_h // 2 - bbox[1]
    draw.text((tx, ty), text, fill=text_color, font=font)
    return pill_w, pill_h


# ── Template renderers ──────────────────────────────────────────────────────


def _render_template_1(
    background: Image.Image,
    headline: str,
    cta: str,
    width: int,
    height: int,
    font_override: str | None,
) -> Image.Image:
    """Image on top, white strip with purple title + pink CTA pill at bottom."""
    design = _DESIGNS[TEMPLATE_1]
    image_h = int(round(height * design.image_cover_frac))
    strip_h = height - image_h

    canvas = Image.new("RGB", (width, height), design.strip_bg or (255, 255, 255))

    # Cover-crop the source image into the upper region.
    fitted = _fit_image_cover(background, width, image_h)
    try:
        canvas.paste(fitted, (0, 0))
    finally:
        fitted.close()

    draw = ImageDraw.Draw(canvas)

    # Accent line directly under the image.
    if design.accent_line_color is not None:
        line_thickness = max(2, height // 360)
        draw.rectangle(
            (0, image_h, width, image_h + line_thickness),
            fill=design.accent_line_color,
        )

    # Title region inside the strip — leave room for the CTA below.
    side_padding = int(width * 0.04)
    title_top = image_h + int(strip_h * 0.10)
    cta_h_reserve = int(strip_h * 0.40)
    title_max_h = strip_h - cta_h_reserve - int(strip_h * 0.10)
    title_max_w = width - side_padding * 2

    font, lines = _fit_title_font(
        draw,
        headline or "",
        title_max_w,
        title_max_h,
        max_lines=2,
        initial_size=int(strip_h * 0.36),
        min_size=max(14, int(strip_h * 0.18)),
        font_override=font_override,
    )

    if lines:
        line_heights = [
            draw.textbbox((0, 0), ln or " ", font=font)[3]
            - draw.textbbox((0, 0), ln or " ", font=font)[1]
            for ln in lines
        ]
        line_spacing = int(font.size * 0.18) if hasattr(font, "size") else 4
        block_h = sum(line_heights) + line_spacing * (len(lines) - 1)
        y = title_top + (title_max_h - block_h) // 2
        for ln, lh in zip(lines, line_heights, strict=True):
            bbox = draw.textbbox((0, 0), ln, font=font)
            tw = bbox[2] - bbox[0]
            tx = (width - tw) // 2 - bbox[0]
            ty = y - bbox[1]
            draw.text((tx, ty), ln, fill=design.title_color, font=font)
            y += lh + line_spacing

    # CTA pill, centered horizontally near the bottom of the strip.
    if cta:
        pill_font = _load_font(
            max(14, int(strip_h * 0.22)), override=font_override
        )
        cta_center_y = image_h + strip_h - int(strip_h * 0.22)
        _draw_pill(
            canvas,
            cta,
            font=pill_font,
            bg=design.cta_bg,
            text_color=design.cta_text_color,
            center_x=width // 2,
            center_y=cta_center_y,
            pad_x=int(width * 0.04),
            pad_y=int(strip_h * 0.06),
            max_width=int(width * 0.85),
        )

    return canvas


def _render_template_2(
    background: Image.Image,
    headline: str,
    cta: str,
    width: int,
    height: int,
    font_override: str | None,
) -> Image.Image:
    """Image fills canvas; green gradient overlay; white title + yellow pill."""
    design = _DESIGNS[TEMPLATE_2]

    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    fitted = _fit_image_cover(background, width, height)
    try:
        canvas.paste(fitted, (0, 0))
    finally:
        fitted.close()

    # Gradient overlay — bright at top, opaque dark green at bottom — applied
    # over the full canvas so the image is gently tinted and the bottom text
    # area is dark enough for white text.
    assert design.gradient_top is not None and design.gradient_bottom is not None
    overlay = _vertical_gradient(
        width, height, design.gradient_top, design.gradient_bottom,
        top_alpha=40, bottom_alpha=235,
    )
    try:
        # Slight blur softens the gradient banding on small canvases.
        soft = overlay.filter(ImageFilter.GaussianBlur(radius=1))
        try:
            canvas_rgba = canvas.convert("RGBA")
            try:
                canvas_rgba.alpha_composite(soft)
                canvas = canvas_rgba.convert("RGB")
            finally:
                canvas_rgba.close()
        finally:
            soft.close()
    finally:
        overlay.close()

    draw = ImageDraw.Draw(canvas)

    # Layout (Yoav 2026-06-08): the CTA pill is anchored to the bottom and
    # spans (nearly) the full canvas width to match the user-supplied mockup;
    # the title block sits bottom-aligned RIGHT ABOVE the pill (no big gap).
    # We compute pill geometry first so the title can be positioned relative
    # to it, then draw both.
    strip_h = int(round(height * design.strip_height_frac))
    side_padding = int(width * 0.03)            # tight padding for near-full-width
    pill_bottom_margin = int(strip_h * 0.06)    # gap between pill and canvas bottom
    title_to_pill_gap = int(strip_h * 0.05)     # gap between title block and pill top

    # Pill geometry (measure first; draw after the title so z-order keeps text
    # legible if anything overlaps).
    pill_font = _load_font(
        max(14, int(strip_h * 0.20)), override=font_override
    )
    pill_pad_x = int(width * 0.04)
    pill_pad_y = int(strip_h * 0.06)
    pill_text = cta or ""
    pill_bbox = draw.textbbox((0, 0), pill_text or "X", font=pill_font)
    pill_text_h = pill_bbox[3] - pill_bbox[1]
    pill_h_actual = pill_text_h + pill_pad_y * 2
    pill_full_width = int(width * 0.94)
    pill_center_y = height - pill_bottom_margin - pill_h_actual // 2
    pill_top = pill_center_y - pill_h_actual // 2

    # Title region: from where the strip starts down to just above the pill.
    title_max_w = width - side_padding * 2
    title_top_bound = height - strip_h + int(strip_h * 0.04)
    title_bottom_bound = pill_top - title_to_pill_gap
    title_max_h = max(int(strip_h * 0.20), title_bottom_bound - title_top_bound)

    font, lines = _fit_title_font(
        draw,
        headline or "",
        title_max_w,
        title_max_h,
        max_lines=2,
        initial_size=int(strip_h * 0.32),
        min_size=max(14, int(strip_h * 0.16)),
        font_override=font_override,
    )

    if lines:
        line_heights = [
            draw.textbbox((0, 0), ln or " ", font=font)[3]
            - draw.textbbox((0, 0), ln or " ", font=font)[1]
            for ln in lines
        ]
        line_spacing = int(font.size * 0.16) if hasattr(font, "size") else 4
        block_h = sum(line_heights) + line_spacing * (len(lines) - 1)
        # Bottom-align the title block to sit right above the pill.
        y = title_bottom_bound - block_h
        for ln, lh in zip(lines, line_heights, strict=True):
            bbox = draw.textbbox((0, 0), ln, font=font)
            tw = bbox[2] - bbox[0]
            tx = (width - tw) // 2 - bbox[0]
            ty = y - bbox[1]
            draw.text((tx, ty), ln, fill=design.title_color, font=font)
            y += lh + line_spacing

    if cta:
        _draw_pill(
            canvas,
            cta,
            font=pill_font,
            bg=design.cta_bg,
            text_color=design.cta_text_color,
            center_x=width // 2,
            center_y=pill_center_y,
            pad_x=pill_pad_x,
            pad_y=pill_pad_y,
            max_width=pill_full_width,
            min_width=pill_full_width,    # force full width (matches mockup)
        )

    return canvas


# ── Public entry ─────────────────────────────────────────────────────────────


_DISPATCH = {
    TEMPLATE_1: _render_template_1,
    TEMPLATE_2: _render_template_2,
}


def render_card_bytes(
    *,
    template_id: str,
    background_image_bytes: bytes,
    headline: str,
    cta: str,
    width: int,
    height: int,
    font_override: str | None = None,
) -> bytes:
    """Render a finished card PNG. Returns bytes ready for upload.

    Raises ``ValueError`` for an unknown template id. Empty ``headline`` is
    allowed (no title drawn). Empty ``cta`` is allowed (no pill drawn — used
    when both the row cell and the per-template default setting are blank).
    """
    if template_id not in SUPPORTED_TEMPLATES:
        raise ValueError(
            f"unknown card template id: {template_id!r}; "
            f"expected one of {sorted(SUPPORTED_TEMPLATES)}"
        )
    if width <= 0 or height <= 0:
        raise ValueError(f"width and height must be positive (got {width}x{height})")

    _log.info(
        "card_render_start",
        template=template_id,
        width=width,
        height=height,
        headline_chars=len(headline or ""),
        cta_chars=len(cta or ""),
    )

    with Image.open(io.BytesIO(background_image_bytes)) as src:
        src.load()
        bg = src.convert("RGB")

    try:
        renderer = _DISPATCH[template_id]
        result = renderer(bg, headline or "", cta or "", width, height, font_override)
        try:
            out = io.BytesIO()
            result.save(out, format="PNG", optimize=True)
            data = out.getvalue()
        finally:
            if result is not bg:
                result.close()
    finally:
        bg.close()

    _log.info(
        "card_render_done",
        template=template_id,
        width=width,
        height=height,
        bytes_out=len(data),
    )
    return data
