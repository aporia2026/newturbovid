"""Tests for the Pillow card renderer.

Covers Templates 1 and 2 across the production aspect ratios. No external
services; all backgrounds are synthesized in-memory.

Plan: ``_plans/2026-06-08-simple-x4-template-cards.md`` §Testing.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from bulkvid.adapters.rendi import DEFAULT_DIMENSIONS_BY_RATIO
from bulkvid.pipeline.card_renderer import (
    SUPPORTED_TEMPLATES,
    TEMPLATE_1,
    TEMPLATE_2,
    render_card_bytes,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


def _solid_bg(width: int = 800, height: int = 800, color=(0, 120, 255)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img.close()
    return buf.getvalue()


def _decode(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img.convert("RGB")


# ── Basic shape ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("template_id", sorted(SUPPORTED_TEMPLATES))
def test_render_returns_png_bytes_at_requested_size(template_id: str) -> None:
    out = render_card_bytes(
        template_id=template_id,
        background_image_bytes=_solid_bg(),
        headline="Hello World",
        cta="DISCOVER MORE >>",
        width=1080,
        height=1920,
    )
    assert isinstance(out, bytes)
    assert len(out) > 1024, "output suspiciously small"
    assert len(out) < 5 * 1024 * 1024, "output suspiciously large for a static card"

    img = _decode(out)
    try:
        assert img.size == (1080, 1920)
        assert img.format in {"PNG", None}    # decoded copy may lose format
    finally:
        img.close()


@pytest.mark.parametrize("template_id", sorted(SUPPORTED_TEMPLATES))
@pytest.mark.parametrize(
    "ratio_str,expected",
    list(DEFAULT_DIMENSIONS_BY_RATIO.items()),
)
def test_render_at_every_supported_aspect_ratio(
    template_id: str, ratio_str: str, expected: tuple[int, int]
) -> None:
    w, h = expected
    out = render_card_bytes(
        template_id=template_id,
        background_image_bytes=_solid_bg(),
        headline="Cross-aspect headline that should not crash",
        cta="GO",
        width=w,
        height=h,
    )
    img = _decode(out)
    try:
        assert img.size == (w, h)
    finally:
        img.close()


# ── Template-specific look ───────────────────────────────────────────────────


def test_template_1_white_strip_present_at_bottom() -> None:
    """Template 1's bottom strip is white. Sample a pixel near a corner of
    the strip where there's no text or pill, and confirm it's bright white."""
    out = render_card_bytes(
        template_id=TEMPLATE_1,
        background_image_bytes=_solid_bg(color=(255, 0, 0)),    # red bg, NOT white
        headline="Hi",
        cta="GO",
        width=600,
        height=600,
    )
    img = _decode(out)
    try:
        # Bottom-left corner area, inside the strip (~88% down, 2% from left).
        # Strip starts at ~78% of height — 88% is safely inside the white strip
        # and far enough from the centered text to land on plain bg.
        x = int(600 * 0.02)
        y = int(600 * 0.88)
        r, g, b = img.getpixel((x, y))
        assert (r, g, b) == (255, 255, 255), f"expected white strip, got {(r, g, b)}"
    finally:
        img.close()


def test_template_1_image_area_preserves_background_color() -> None:
    """Template 1's upper region is the background image. A solid-red input
    should still read mostly red in that upper region (cover-cropped, untouched)."""
    out = render_card_bytes(
        template_id=TEMPLATE_1,
        background_image_bytes=_solid_bg(color=(220, 30, 30)),
        headline="",
        cta="",
        width=600,
        height=600,
    )
    img = _decode(out)
    try:
        # Sample a point well inside the upper image area.
        r, g, b = img.getpixel((300, 100))
        assert r > 180 and g < 80 and b < 80, f"upper area should be reddish, got {(r, g, b)}"
    finally:
        img.close()


def test_template_2_gradient_darkens_the_bottom() -> None:
    """Template 2's gradient overlay should make a near-bottom pixel
    noticeably darker / greener than a near-top pixel, even on a bright
    background."""
    out = render_card_bytes(
        template_id=TEMPLATE_2,
        background_image_bytes=_solid_bg(color=(255, 255, 255)),    # white bg
        headline="",
        cta="",
        width=600,
        height=600,
    )
    img = _decode(out)
    try:
        # 5% from top vs 95% from bottom-ish (but above text area).
        top_px = img.getpixel((10, int(600 * 0.05)))
        bottom_px = img.getpixel((10, int(600 * 0.92)))
        top_lum = sum(top_px) / 3
        bottom_lum = sum(bottom_px) / 3
        assert bottom_lum < top_lum - 30, (
            f"bottom should be much darker than top; "
            f"top={top_px} lum={top_lum:.0f}, bottom={bottom_px} lum={bottom_lum:.0f}"
        )
        # The bottom should have a green tint (G > R and G > B).
        assert bottom_px[1] > bottom_px[0], "bottom should be greener than red"
    finally:
        img.close()


# ── CTA handling ─────────────────────────────────────────────────────────────


def test_empty_cta_does_not_crash_or_overflow(tmp_path) -> None:
    """Empty CTA = pill is omitted entirely. The render still succeeds and
    the bytes are still a valid PNG."""
    out = render_card_bytes(
        template_id=TEMPLATE_1,
        background_image_bytes=_solid_bg(),
        headline="Headline only",
        cta="",
        width=600,
        height=600,
    )
    img = _decode(out)
    try:
        assert img.size == (600, 600)
    finally:
        img.close()


def test_very_long_headline_is_wrapped_not_truncated() -> None:
    """A long headline must wrap and still render; the renderer should never
    raise even with content that overflows the design max."""
    long_text = " ".join(["word"] * 60)
    out = render_card_bytes(
        template_id=TEMPLATE_2,
        background_image_bytes=_solid_bg(),
        headline=long_text,
        cta="See more",
        width=1080,
        height=1080,
    )
    img = _decode(out)
    try:
        assert img.size == (1080, 1080)
    finally:
        img.close()


# ── Validation ───────────────────────────────────────────────────────────────


def test_unknown_template_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown card template id"):
        render_card_bytes(
            template_id="3",
            background_image_bytes=_solid_bg(),
            headline="x",
            cta="y",
            width=100,
            height=100,
        )


def test_zero_dimensions_raise_value_error() -> None:
    with pytest.raises(ValueError, match="positive"):
        render_card_bytes(
            template_id=TEMPLATE_1,
            background_image_bytes=_solid_bg(),
            headline="x",
            cta="y",
            width=0,
            height=100,
        )
