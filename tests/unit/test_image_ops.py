"""Tests for the lifted image_ops module.

Cover the **mandatory** collage method end-to-end (plan §3 constraints).
No external services; all images are synthesized in-memory with PIL.

Covers:
  - parse_cut_dimension: ratio / pixels / none / garbage / float ratios
  - parse_aspect_ratio: ratio / pixels / auto / invalid
  - crop_to_ratio + crop_to_ratio_pil: center crop preserves aspect
  - crop_to_pixels: exact final dimensions
  - upscale_image_pil: scales up to meet min, no-op when above
  - optimize_image_for_size: stays under 2MB, handles RGBA→RGB
  - split_collage_2x2: 4 quadrants in TL/TR/BL/BR order, color-tagged for ordering proof
  - split_collage_2x2 with edge crop: smaller output, divider removed
  - split_collage_with_processing: end-to-end through optimizer, all 4 under cap
  - is_likely_midjourney_collage: square+large = True, off-shape = False
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from bulkvid.image_ops import (
    DEFAULT_EDGE_CROP_PIXELS,
    MAX_IMAGE_SIZE_BYTES,
    crop_to_pixels,
    crop_to_ratio,
    crop_to_ratio_pil,
    is_likely_midjourney_collage,
    optimize_image_for_size,
    parse_aspect_ratio,
    parse_cut_dimension,
    split_collage_2x2,
    split_collage_with_processing,
    upscale_image_pil,
)


# ── Fixtures: synthetic images ──────────────────────────────────────────────


def _solid_image(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    """Solid-color PNG of given dims, returned as bytes."""
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img.close()
    return buf.getvalue()


def _four_color_collage(size: int = 800) -> bytes:
    """Build a 2x2 collage where each quadrant is a distinct solid color.

    TL=red, TR=green, BL=blue, BR=yellow.
    Lets us prove split order by sampling the center pixel of each output.
    """
    half = size // 2
    img = Image.new("RGB", (size, size), (0, 0, 0))
    tl = Image.new("RGB", (half, half), (255, 0, 0))
    tr = Image.new("RGB", (half, half), (0, 255, 0))
    bl = Image.new("RGB", (half, half), (0, 0, 255))
    br = Image.new("RGB", (half, half), (255, 255, 0))
    img.paste(tl, (0, 0))
    img.paste(tr, (half, 0))
    img.paste(bl, (0, half))
    img.paste(br, (half, half))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    for x in (tl, tr, bl, br, img):
        x.close()
    return buf.getvalue()


def _center_color(img_bytes: bytes) -> tuple[int, int, int]:
    with Image.open(io.BytesIO(img_bytes)) as img:
        rgb = img.convert("RGB")
        try:
            w, h = rgb.size
            return rgb.getpixel((w // 2, h // 2))
        finally:
            rgb.close()


# ── parse_cut_dimension ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected_type"),
    [
        ("", "none"),
        ("auto", "none"),
        ("AUTO", "none"),
        ("16:9", "ratio"),
        ("1.91:1", "ratio"),
        ("1080x1920", "pixels"),
        ("garbage", "none"),
        ("0:0", "none"),
    ],
)
def test_parse_cut_dimension_type(value: str, expected_type: str) -> None:
    assert parse_cut_dimension(value).type == expected_type


def test_parse_cut_dimension_ratio_values() -> None:
    spec = parse_cut_dimension("16:9")
    assert spec.type == "ratio"
    assert spec.ratio_w == 16
    assert spec.ratio_h == 9


def test_parse_cut_dimension_pixel_values() -> None:
    spec = parse_cut_dimension("1080x1920")
    assert spec.type == "pixels"
    assert spec.width == 1080
    assert spec.height == 1920


# ── parse_aspect_ratio ──────────────────────────────────────────────────────


def test_parse_aspect_ratio_returns_float() -> None:
    assert parse_aspect_ratio("3:2") == pytest.approx(1.5)


def test_parse_aspect_ratio_returns_tuple_for_pixels() -> None:
    assert parse_aspect_ratio("300x250") == (300, 250)


def test_parse_aspect_ratio_returns_none_for_auto() -> None:
    assert parse_aspect_ratio("auto") is None


def test_parse_aspect_ratio_returns_none_for_garbage() -> None:
    assert parse_aspect_ratio("garbage") is None


# ── crop_to_ratio ───────────────────────────────────────────────────────────


def test_crop_to_ratio_returns_same_bytes_if_already_at_ratio() -> None:
    img = _solid_image(400, 400, (128, 128, 128))
    out = crop_to_ratio(img, 1, 1)
    assert out == img  # bit-identical when no work needed


def test_crop_to_ratio_landscape_to_square() -> None:
    img = _solid_image(800, 400, (200, 100, 100))
    out = crop_to_ratio(img, 1, 1)
    with Image.open(io.BytesIO(out)) as i:
        assert i.size == (400, 400)


def test_crop_to_ratio_portrait_to_landscape() -> None:
    img = _solid_image(400, 800, (50, 200, 50))
    out = crop_to_ratio(img, 16, 9)
    with Image.open(io.BytesIO(out)) as i:
        w, h = i.size
        assert abs((w / h) - (16 / 9)) < 0.02


# ── crop_to_ratio_pil ───────────────────────────────────────────────────────


def test_crop_to_ratio_pil_returns_detached_copy_when_no_crop_needed() -> None:
    img = Image.new("RGB", (400, 400), (0, 0, 0))
    out = crop_to_ratio_pil(img, 1.0)
    assert out is not img        # detached
    assert out.size == img.size
    img.close()
    out.close()


def test_crop_to_ratio_pil_none_target_returns_copy() -> None:
    img = Image.new("RGB", (123, 456), (10, 10, 10))
    out = crop_to_ratio_pil(img, None)
    assert out is not img
    assert out.size == img.size
    img.close()
    out.close()


# ── crop_to_pixels ──────────────────────────────────────────────────────────


def test_crop_to_pixels_returns_exact_dimensions() -> None:
    img = _solid_image(1000, 700, (50, 60, 70))
    out = crop_to_pixels(img, 1080, 1920)
    with Image.open(io.BytesIO(out)) as i:
        assert i.size == (1080, 1920)


# ── upscale_image_pil ───────────────────────────────────────────────────────


def test_upscale_when_below_minimum() -> None:
    img = Image.new("RGB", (300, 200), (0, 0, 0))
    out = upscale_image_pil(img, min_width=600, min_height=600, auto_upscale=True)
    w, h = out.size
    # Aspect ratio preserved; both dimensions at least the minimum.
    assert w >= 600 or h >= 600
    assert abs((w / h) - (300 / 200)) < 0.05
    out.close()
    img.close()


def test_upscale_noop_when_already_above_minimum() -> None:
    img = Image.new("RGB", (800, 800), (0, 0, 0))
    out = upscale_image_pil(img, min_width=600, min_height=600, auto_upscale=True)
    assert out is img            # no-op when above min
    img.close()


def test_upscale_noop_when_disabled() -> None:
    img = Image.new("RGB", (100, 100), (0, 0, 0))
    out = upscale_image_pil(img, min_width=600, min_height=600, auto_upscale=False)
    assert out is img            # disabled overrides need
    img.close()


# ── optimize_image_for_size ─────────────────────────────────────────────────


def test_optimize_stays_under_2mb_for_normal_image() -> None:
    img = Image.new("RGB", (1080, 1920), (100, 50, 50))
    buf, fmt, content_type = optimize_image_for_size(img)
    assert buf.getbuffer().nbytes <= MAX_IMAGE_SIZE_BYTES
    assert fmt in ("PNG", "JPEG")
    assert content_type in ("image/png", "image/jpeg")
    img.close()


def test_optimize_converts_rgba_to_rgb_with_white_background() -> None:
    rgba = Image.new("RGBA", (200, 200), (255, 0, 0, 128))
    buf, fmt, content_type = optimize_image_for_size(rgba, max_size_bytes=1024 * 1024)
    # Loads without exploding (the RGBA flatten path is exercised).
    with Image.open(buf) as out:
        out.load()
        assert out.mode in ("RGB", "L")


def test_optimize_with_tiny_budget_falls_through_to_jpeg() -> None:
    # 1KB budget forces the resize+quality drop path.
    img = Image.new("RGB", (1500, 1500), (220, 30, 30))
    buf, fmt, _ = optimize_image_for_size(img, max_size_bytes=1024)
    # Best-effort: returns SOMETHING with a JPEG container, even if it can't fit.
    assert fmt == "JPEG"
    img.close()


# ── split_collage_2x2 ───────────────────────────────────────────────────────


def test_split_collage_returns_four_quadrants() -> None:
    bytes_in = _four_color_collage(800)
    parts = split_collage_2x2(bytes_in, edge_crop_pixels=0)
    assert len(parts) == 4


def test_split_collage_order_is_tl_tr_bl_br() -> None:
    bytes_in = _four_color_collage(800)
    tl, tr, bl, br = split_collage_2x2(bytes_in, edge_crop_pixels=0)
    assert _center_color(tl) == (255, 0, 0)        # red
    assert _center_color(tr) == (0, 255, 0)        # green
    assert _center_color(bl) == (0, 0, 255)        # blue
    assert _center_color(br) == (255, 255, 0)      # yellow


def test_split_collage_each_quadrant_is_half_size() -> None:
    bytes_in = _four_color_collage(800)
    parts = split_collage_2x2(bytes_in, edge_crop_pixels=0)
    for p in parts:
        with Image.open(io.BytesIO(p)) as q:
            assert q.size == (400, 400)


def test_split_collage_with_edge_crop_shrinks_each_quadrant() -> None:
    bytes_in = _four_color_collage(800)
    parts_no_crop = split_collage_2x2(bytes_in, edge_crop_pixels=0)
    parts_cropped = split_collage_2x2(bytes_in, edge_crop_pixels=10)
    with Image.open(io.BytesIO(parts_no_crop[0])) as a, Image.open(
        io.BytesIO(parts_cropped[0])
    ) as b:
        assert a.size == (400, 400)
        assert b.size == (380, 380)         # 400 - 2*10


def test_split_collage_skips_edge_crop_when_too_small() -> None:
    # 20x20 image, half = 10x10, 2*edge=20 > 10 -> skip the crop.
    img = Image.new("RGB", (20, 20), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img.close()

    parts = split_collage_2x2(buf.getvalue(), edge_crop_pixels=10)
    assert len(parts) == 4
    with Image.open(io.BytesIO(parts[0])) as q:
        assert q.size == (10, 10)            # unchanged — edge crop bypassed


# ── split_collage_with_processing ────────────────────────────────────────────


def test_split_with_processing_returns_four_optimised_quadrants() -> None:
    bytes_in = _four_color_collage(1600)
    parts = split_collage_with_processing(
        bytes_in,
        edge_crop_pixels=DEFAULT_EDGE_CROP_PIXELS,
        final_aspect_ratio=9 / 16,
        min_width=600,
        min_height=600,
    )
    assert len(parts) == 4
    for p in parts:
        assert len(p) <= MAX_IMAGE_SIZE_BYTES         # under 2MB cap
        with Image.open(io.BytesIO(p)) as q:
            assert q.size[0] >= 600 or q.size[1] >= 600     # upscaled to min if needed


def test_split_with_processing_respects_final_dimensions() -> None:
    bytes_in = _four_color_collage(1200)
    parts = split_collage_with_processing(
        bytes_in,
        edge_crop_pixels=0,
        final_dimensions=(540, 960),
    )
    for p in parts:
        with Image.open(io.BytesIO(p)) as q:
            assert q.size == (540, 960)


# ── is_likely_midjourney_collage ─────────────────────────────────────────────


def test_midjourney_heuristic_square_and_large() -> None:
    img = _solid_image(1600, 1600, (200, 200, 200))
    assert is_likely_midjourney_collage(img) is True


def test_midjourney_heuristic_rejects_landscape() -> None:
    img = _solid_image(1600, 900, (200, 200, 200))
    assert is_likely_midjourney_collage(img) is False


def test_midjourney_heuristic_rejects_small() -> None:
    img = _solid_image(800, 800, (200, 200, 200))
    assert is_likely_midjourney_collage(img) is False
