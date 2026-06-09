"""Tests for the Pillow text-on-image overlay used by the
``paste text on img`` tab. No external services; all backgrounds are
synthesized in-memory."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from bulkvid.adapters.rendi import DEFAULT_DIMENSIONS_BY_RATIO
from bulkvid.pipeline.text_overlay import overlay_text_on_image_bytes


def _src_png(width: int = 1600, height: int = 900, color=(120, 180, 220)) -> bytes:
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


@pytest.mark.parametrize(
    "ratio,expected",
    sorted(DEFAULT_DIMENSIONS_BY_RATIO.items()),
)
def test_overlay_returns_target_canvas_size(
    ratio: str, expected: tuple[int, int]
) -> None:
    """The output PNG must land at the row's target aspect dimensions —
    the blurred-background fit composites the (preserved) source inside.
    Text geometry is then computed on the target canvas so visual text
    size is consistent across rows regardless of source aspect."""
    w, h = expected
    out = overlay_text_on_image_bytes(
        _src_png(),
        "Casas embargadas: precios y oportunidades",
        aspect_ratio=ratio,
    )
    assert isinstance(out, bytes)
    assert len(out) > 1024, "output suspiciously small"
    img = _decode(out)
    try:
        assert img.size == (w, h)
    finally:
        img.close()


@pytest.mark.parametrize(
    "src_size",
    [(1600, 900), (1080, 1080), (900, 1600), (2400, 1600)],
)
def test_overlay_at_916_target_always_lands_at_1080x1920(
    src_size: tuple[int, int],
) -> None:
    """v3 regression: a landscape source got tiny text after Rendi
    letterboxed it. Now every source — landscape, square, portrait —
    composites into the same 1080×1920 canvas and the text is rendered
    on that canvas, so visual text size is consistent."""
    sw, sh = src_size
    out = overlay_text_on_image_bytes(
        _src_png(width=sw, height=sh),
        "Hello World",
        aspect_ratio="9:16",
    )
    img = _decode(out)
    try:
        assert img.size == (1080, 1920), (
            f"src {src_size} → output {img.size}, expected (1080, 1920)"
        )
    finally:
        img.close()


# ── Blank text branch ──────────────────────────────────────────────────────


def test_blank_text_returns_image_without_overlay() -> None:
    """Empty / whitespace-only text must still produce a valid PNG (no
    text drawn) instead of erroring — Apps Script may submit a row with
    a blank Text cell and we want the row to still ship a clean video."""
    out = overlay_text_on_image_bytes(
        _src_png(color=(220, 30, 30)),    # solid red
        "",
        aspect_ratio="9:16",
    )
    img = _decode(out)
    try:
        # No text drawn — the cover-cropped image fills the canvas, so a
        # sample pixel reads pure red (the source color).
        r, g, b = img.getpixel((img.width // 2, img.height // 2))
        assert r > 180 and g < 80 and b < 80, (
            f"expected solid red source to show through, got {(r, g, b)}"
        )
    finally:
        img.close()


def test_whitespace_only_text_treated_as_blank() -> None:
    """The ``text.strip()`` in the renderer must collapse pure-whitespace
    input to the blank path — otherwise we'd render an invisible block
    AND skip the early return, wasting CPU."""
    out = overlay_text_on_image_bytes(
        _src_png(color=(220, 30, 30)),
        "   \n\t  ",
        aspect_ratio="9:16",
    )
    img = _decode(out)
    try:
        r, g, b = img.getpixel((img.width // 2, img.height // 2))
        assert r > 180 and g < 80 and b < 80
    finally:
        img.close()


# ── Real overlay actually changes pixels ───────────────────────────────────


def test_overlay_draws_white_text_with_black_outline() -> None:
    """The reference design is white fill + thick black stroke. Center
    the text on a solid red source and confirm that:
      * the canvas contains some pure-white pixels (the text fill)
      * the canvas contains some near-black pixels (the stroke)
    Without the overlay, neither colour would appear on a red canvas."""
    out = overlay_text_on_image_bytes(
        _src_png(color=(220, 30, 30)),
        "Hello World",
        aspect_ratio="9:16",
    )
    img = _decode(out)
    try:
        # Walk every pixel once — fine at 1080×1920 in tests.
        pixels = img.load()
        assert pixels is not None
        saw_white = False
        saw_black = False
        for y in range(0, img.height, 8):
            for x in range(0, img.width, 8):
                r, g, b = pixels[x, y]
                if r > 240 and g > 240 and b > 240:
                    saw_white = True
                if r < 30 and g < 30 and b < 30:
                    saw_black = True
                if saw_white and saw_black:
                    break
            if saw_white and saw_black:
                break
        assert saw_white, "expected white fill pixels from the text overlay"
        assert saw_black, "expected black stroke pixels around the text"
    finally:
        img.close()


# ── Long-text auto-shrink ─────────────────────────────────────────────────


def test_overlay_long_text_does_not_overflow_horizontally() -> None:
    """A long single-language word like 'oportunidades' at the initial
    font size would overflow the canvas width. The width-per-line check
    in ``_fit_overlay_font`` has to shrink the font until every wrapped
    line fits. Without the check, the text rendered off the canvas."""
    out = overlay_text_on_image_bytes(
        _src_png(),
        "Oportunidades extraordinarias para todos",
        aspect_ratio="9:16",
    )
    img = _decode(out)
    try:
        # Walk the leftmost 2% and rightmost 2% of the canvas. The overlay
        # must not have drawn any pure-white pixels there — those margins
        # are inside the side padding, so seeing white text would mean we
        # overshot the safe zone.
        pixels = img.load()
        assert pixels is not None
        left_band = int(img.width * 0.02)
        right_band = int(img.width * 0.98)
        for y in range(0, img.height, 16):
            r, g, b = pixels[left_band, y]
            assert not (r > 240 and g > 240 and b > 240), (
                f"left-edge column {left_band} got white at y={y} — text overflowed"
            )
            r, g, b = pixels[right_band, y]
            assert not (r > 240 and g > 240 and b > 240), (
                f"right-edge column {right_band} got white at y={y} — text overflowed"
            )
    finally:
        img.close()
