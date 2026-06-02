"""Image processing — lifted from creative_builder_dev with mandatory parity.

Source: ``refs/creative_builder_dev/core/image/image_ops.py`` (production-tested).

The collage-split method is **mandatory** (plan §3 constraints, §5 "Per-row
pipeline (Image-VO tab)" step 7). The functions here implement exactly the
same TL/TR/BL/BR split + edge-crop + aspect-ratio crop + 2MB optimizer that
the existing Aporia pipeline uses, so we get bit-identical output for any
identical inputs.

Public surface
--------------
- ``CutSpec`` / ``parse_cut_dimension`` / ``parse_aspect_ratio``  — sheet inputs
- ``crop_to_ratio``       — bytes -> bytes, center crop to W:H
- ``crop_to_ratio_pil``   — PIL -> PIL, center crop to a float ratio
- ``crop_to_pixels``      — bytes -> bytes, crop+resize to exact W,H
- ``upscale_image_pil``   — bring under-sized images up to a min
- ``optimize_image_for_size`` — 2MB target, PNG -> JPEG@95 -> resize -> drop quality
- ``split_collage_2x2``   — the mandatory 2x2 split with edge crop
- ``split_collage_with_processing`` — the all-in-one production pipeline

Plan: ``_plans/2026-06-02-aporia-bulk-video-tool.md`` §3, §5, §10 ("image_ops" tests).
"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from PIL import Image

from bulkvid.logging import get_logger

_log = get_logger("imageops")


# ── Constants ────────────────────────────────────────────────────────────────
# Production tuning (parity with refs/creative_builder_dev/core/image/image_ops.py).
MAX_IMAGE_SIZE_BYTES = 2 * 1024 * 1024            # 2 MB cap
MIN_DIMENSION_FOR_OPTIMIZATION = 800              # don't shrink below this when optimising
DEFAULT_EDGE_CROP_PIXELS = 10                     # quadrant divider removal


# ── Cut specs (sheet input parsing) ──────────────────────────────────────────


@dataclass(frozen=True)
class CutSpec:
    """Parsed cut dimension specification (from the Sheet's Change Size cell)."""

    type: Literal["ratio", "pixels", "none"]
    width: int | None = None
    height: int | None = None
    ratio_w: float | None = None
    ratio_h: float | None = None


def parse_cut_dimension(value: str) -> CutSpec:
    """Parse a Sheet cell into a CutSpec.

    Accepts:
      - W:H ratios, possibly floats (``"16:9"``, ``"1.91:1"``)
      - WxH pixels (``"1080x1920"``)
      - empty / ``"auto"`` -> ``type='none'``
    Unrecognised input is logged and returns ``type='none'``.
    """
    v = (value or "").strip()
    if not v or v.lower() == "auto":
        return CutSpec(type="none")

    if ":" in v and "x" not in v.lower():
        parts = v.split(":")
        if len(parts) == 2:
            try:
                w = float(parts[0].strip())
                h = float(parts[1].strip())
                if w > 0 and h > 0:
                    return CutSpec(type="ratio", ratio_w=w, ratio_h=h)
            except ValueError:
                pass

    if "x" in v.lower():
        parts = v.lower().split("x")
        if len(parts) == 2:
            try:
                w = int(parts[0].strip())
                h = int(parts[1].strip())
                if w > 0 and h > 0:
                    return CutSpec(type="pixels", width=w, height=h)
            except ValueError:
                pass

    _log.warning("cut_parse_failed", value=v)
    return CutSpec(type="none")


def parse_aspect_ratio(ratio_string: str) -> float | tuple[int, int] | None:
    """Parse a ratio string.

    Returns:
      - ``None`` for ``"auto"`` or invalid
      - ``float`` (W/H) for ``"3:2"``
      - ``(width, height)`` tuple for ``"300x250"``
    """
    if not ratio_string or ratio_string.lower() == "auto":
        return None
    s = ratio_string.strip()
    if "x" in s.lower():
        try:
            parts = s.lower().split("x")
            if len(parts) == 2:
                w = int(parts[0].strip())
                h = int(parts[1].strip())
                if w > 0 and h > 0:
                    return (w, h)
        except (ValueError, IndexError):
            return None
    if ":" in s:
        try:
            parts = s.split(":")
            if len(parts) == 2:
                w = float(parts[0])
                h = float(parts[1])
                if h > 0:
                    return w / h
        except (ValueError, ZeroDivisionError):
            return None
    return None


# ── Naming helpers (kept for parity with the existing storage layout) ────────


def generate_random_id(length: int = 6) -> str:
    return "".join(str(random.randint(0, 9)) for _ in range(length))


def get_date_str() -> str:
    """Reference format: DDMMYY."""
    return datetime.now().strftime("%d%m%y")


# ── Crops ────────────────────────────────────────────────────────────────────


def crop_to_ratio(img_bytes: bytes, ratio_w: float, ratio_h: float) -> bytes:
    """Center-crop bytes to ``ratio_w:ratio_h``. Returns bytes (same format)."""
    img = Image.open(io.BytesIO(img_bytes))
    img.load()
    try:
        orig_w, orig_h = img.size
        target_aspect = ratio_w / ratio_h
        current_aspect = orig_w / orig_h

        if abs(target_aspect - current_aspect) < 0.01:
            return img_bytes

        if current_aspect > target_aspect:
            new_w = int(orig_h * target_aspect)
            new_h = orig_h
            left = (orig_w - new_w) // 2
            top = 0
        else:
            new_w = orig_w
            new_h = int(orig_w / target_aspect)
            left = 0
            top = (orig_h - new_h) // 2

        right = left + new_w
        bottom = top + new_h
        cropped = img.crop((left, top, right, bottom))
        try:
            out = io.BytesIO()
            fmt = img.format or "PNG"
            cropped.save(out, format=fmt)
            return out.getvalue()
        finally:
            cropped.close()
    finally:
        img.close()


def crop_to_ratio_pil(image: Image.Image, target_ratio: float | None) -> Image.Image:
    """Center-crop PIL Image to ``target_ratio`` (W/H). Returns a detached copy."""
    if target_ratio is None:
        return image.copy()

    width, height = image.size
    current_ratio = width / height
    if abs(current_ratio - target_ratio) < 0.01:
        return image.copy()

    if current_ratio > target_ratio:
        new_width = int(height * target_ratio)
        new_height = height
        left = (width - new_width) // 2
        top = 0
    else:
        new_width = width
        new_height = int(width / target_ratio)
        left = 0
        top = (height - new_height) // 2

    right = left + new_width
    bottom = top + new_height
    cropped = image.crop((left, top, right, bottom))
    try:
        return cropped.copy()
    finally:
        cropped.close()


def crop_to_pixels(img_bytes: bytes, width: int, height: int) -> bytes:
    """Center-crop to the W:H ratio then resize to exactly ``width`` × ``height``."""
    cropped_bytes = crop_to_ratio(img_bytes, width, height)
    img = Image.open(io.BytesIO(cropped_bytes))
    try:
        resized = img.resize((width, height), Image.Resampling.LANCZOS)
        try:
            out = io.BytesIO()
            fmt = img.format or "PNG"
            resized.save(out, format=fmt)
            return out.getvalue()
        finally:
            resized.close()
    finally:
        img.close()


# ── Upscale ──────────────────────────────────────────────────────────────────


def upscale_image_pil(
    image: Image.Image, min_width: int, min_height: int, auto_upscale: bool
) -> Image.Image:
    """Upscale a PIL image to at least ``min_width`` × ``min_height`` if enabled."""
    width, height = image.size
    needs_upscale = width < min_width or height < min_height
    if not (needs_upscale and auto_upscale):
        return image

    scale_w = min_width / width if width < min_width else 1
    scale_h = min_height / height if height < min_height else 1
    scale = max(scale_w, scale_h)
    new_w = int(width * scale)
    new_h = int(height * scale)
    upscaled = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    _log.debug(
        "image_upscaled",
        original=f"{width}x{height}",
        new=f"{new_w}x{new_h}",
    )
    return upscaled


# ── Optimizer (2MB cap) ─────────────────────────────────────────────────────


def optimize_image_for_size(
    image: Image.Image,
    max_size_bytes: int = MAX_IMAGE_SIZE_BYTES,
    min_dimension: int = MIN_DIMENSION_FOR_OPTIMIZATION,
    preserve_exact_size: bool = False,
) -> tuple[io.BytesIO, str, str]:
    """Compress under ``max_size_bytes`` while preserving aspect ratio.

    Strategy (parity with reference):
      1. PNG (best quality)        — if under limit, ship it
      2. JPEG quality 95           — if under limit, ship it
      3. ``preserve_exact_size``: only drop JPEG quality, no resize
      4. Otherwise: scale 10% per pass down to ``min_dimension`` on shortest side
      5. At min dimensions: drop JPEG quality
      6. Last resort: emergency scale below min_dimension

    Returns ``(buffer, format, content_type)``.
    """
    width, height = image.size
    original_width, original_height = width, height
    aspect_ratio = width / height

    # Convert to RGB for JPEG compatibility, flatten transparency on white.
    if image.mode in ("RGBA", "P", "LA"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode == "P":
            image = image.convert("RGBA")
        if image.mode in ("RGBA", "LA"):
            if image.mode == "LA":
                image = image.convert("RGBA")
            background.paste(image, mask=image.split()[-1])
        else:
            background.paste(image)
        try:
            image.close()
        except Exception:
            pass
        image = background
    elif image.mode != "RGB":
        converted = image.convert("RGB")
        try:
            image.close()
        except Exception:
            pass
        image = converted

    img_buffer = io.BytesIO()

    def _reset_buf() -> None:
        img_buffer.seek(0)
        img_buffer.truncate(0)

    # Step 1: PNG
    _reset_buf()
    image.save(img_buffer, format="PNG", optimize=True)
    if img_buffer.tell() <= max_size_bytes:
        img_buffer.seek(0)
        return img_buffer, "PNG", "image/png"

    # Step 2: JPEG q=95
    _reset_buf()
    image.save(img_buffer, format="JPEG", quality=95, optimize=True)
    if img_buffer.tell() <= max_size_bytes:
        img_buffer.seek(0)
        return img_buffer, "JPEG", "image/jpeg"

    if preserve_exact_size:
        for quality in (90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 25, 20):
            _reset_buf()
            image.save(img_buffer, format="JPEG", quality=quality, optimize=True)
            if img_buffer.tell() <= max_size_bytes:
                img_buffer.seek(0)
                return img_buffer, "JPEG", "image/jpeg"
        img_buffer.seek(0)
        return img_buffer, "JPEG", "image/jpeg"

    # Step 3: progressive resize until min_dimension.
    if aspect_ratio >= 1:
        min_height_target = min_dimension
        min_width_target = int(min_dimension * aspect_ratio)
    else:
        min_width_target = min_dimension
        min_height_target = int(min_dimension / aspect_ratio)

    current_image = image
    current_width, current_height = width, height
    scale_step = 0.9

    while current_width > min_width_target and current_height > min_height_target:
        new_width = int(current_width * scale_step)
        new_height = int(current_height * scale_step)
        if new_width < min_width_target or new_height < min_height_target:
            new_width = min_width_target
            new_height = min_height_target

        # Resize from ORIGINAL each pass (avoid compounding resampling loss).
        resized = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
        if current_image is not image:
            try:
                current_image.close()
            except Exception:
                pass
        current_image = resized
        current_width, current_height = new_width, new_height

        _reset_buf()
        current_image.save(img_buffer, format="JPEG", quality=90, optimize=True)
        if img_buffer.tell() <= max_size_bytes:
            _log.debug(
                "image_resized",
                original=f"{original_width}x{original_height}",
                new=f"{new_width}x{new_height}",
            )
            img_buffer.seek(0)
            return img_buffer, "JPEG", "image/jpeg"

    # Step 4: at min dims, drop quality.
    if current_image is not image:
        try:
            current_image.close()
        except Exception:
            pass
    current_image = image.resize((min_width_target, min_height_target), Image.Resampling.LANCZOS)

    for quality in (85, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30):
        _reset_buf()
        current_image.save(img_buffer, format="JPEG", quality=quality, optimize=True)
        if img_buffer.tell() <= max_size_bytes:
            _log.debug(
                "image_optimized",
                original=f"{original_width}x{original_height}",
                new=f"{min_width_target}x{min_height_target}",
                quality=quality,
            )
            img_buffer.seek(0)
            return img_buffer, "JPEG", "image/jpeg"

    # Step 5: emergency scale below the floor.
    for scale in (0.8, 0.6, 0.5, 0.4):
        ew = int(min_width_target * scale)
        eh = int(min_height_target * scale)
        try:
            current_image.close()
        except Exception:
            pass
        current_image = image.resize((ew, eh), Image.Resampling.LANCZOS)
        _reset_buf()
        current_image.save(img_buffer, format="JPEG", quality=50, optimize=True)
        if img_buffer.tell() <= max_size_bytes:
            _log.warning(
                "image_emergency_resize",
                original=f"{original_width}x{original_height}",
                new=f"{ew}x{eh}",
            )
            img_buffer.seek(0)
            return img_buffer, "JPEG", "image/jpeg"

    _log.warning(
        "image_optimization_failed",
        max_size_mb=round(max_size_bytes / 1024 / 1024, 2),
    )
    img_buffer.seek(0)
    return img_buffer, "JPEG", "image/jpeg"


# ── The mandatory 2x2 split ──────────────────────────────────────────────────


def split_collage_2x2(img_bytes: bytes, edge_crop_pixels: int = 0) -> list[bytes]:
    """Split a 2x2 collage into 4 quadrants WITH optional edge crop.

    Order is **deterministic**: Top-Left, Top-Right, Bottom-Left, Bottom-Right.
    Edge crop removes the divider strip generated by the upstream image model.

    Quadrants are processed one at a time to bound peak memory under
    parallel workloads (plan §10 perf).
    """
    with Image.open(io.BytesIO(img_bytes)) as img:
        w, h = img.size
        half_w = w // 2
        half_h = h // 2
        fmt = img.format or "PNG"

        boxes = [
            (0, 0, half_w, half_h),           # TL
            (half_w, 0, w, half_h),           # TR
            (0, half_h, half_w, h),           # BL
            (half_w, half_h, w, h),           # BR
        ]

        results: list[bytes] = []
        for box in boxes:
            quad = img.crop(box)
            try:
                qw, qh = quad.size
                if (
                    edge_crop_pixels > 0
                    and qw > 2 * edge_crop_pixels
                    and qh > 2 * edge_crop_pixels
                ):
                    cropped = quad.crop(
                        (
                            edge_crop_pixels,
                            edge_crop_pixels,
                            qw - edge_crop_pixels,
                            qh - edge_crop_pixels,
                        )
                    )
                    try:
                        out = io.BytesIO()
                        cropped.save(out, format=fmt)
                        results.append(out.getvalue())
                    finally:
                        cropped.close()
                else:
                    out = io.BytesIO()
                    quad.save(out, format=fmt)
                    results.append(out.getvalue())
            finally:
                quad.close()
        return results


def split_collage_with_processing(
    img_bytes: bytes,
    edge_crop_pixels: int = DEFAULT_EDGE_CROP_PIXELS,
    final_aspect_ratio: float | None = None,
    final_dimensions: tuple[int, int] | None = None,
    min_width: int = 600,
    min_height: int = 600,
    auto_upscale: bool = True,
) -> list[bytes]:
    """Full production split: 4 quadrants, edge crop, aspect crop, optional pixel cut,
    upscale, optimize.

    Returns 4 optimised image bytes (under 2MB each).
    """

    def _process_one(quad: Image.Image) -> bytes:
        preserve_exact_size = False
        cur = quad
        try:
            # 1) Edge crop
            qw, qh = cur.size
            if (
                edge_crop_pixels > 0
                and qw > 2 * edge_crop_pixels
                and qh > 2 * edge_crop_pixels
            ):
                nxt = cur.crop(
                    (
                        edge_crop_pixels,
                        edge_crop_pixels,
                        qw - edge_crop_pixels,
                        qh - edge_crop_pixels,
                    )
                )
                if nxt is not cur:
                    try:
                        cur.close()
                    except Exception:
                        pass
                cur = nxt

            # 2) Final aspect ratio crop
            if final_aspect_ratio is not None:
                nxt = crop_to_ratio_pil(cur, final_aspect_ratio)
                if nxt is not cur:
                    try:
                        cur.close()
                    except Exception:
                        pass
                cur = nxt

            # 3) Optional resize to exact dimensions
            if final_dimensions is not None:
                tw, th = final_dimensions
                if tw > 0 and th > 0:
                    nxt = cur.resize((tw, th), Image.Resampling.LANCZOS)
                    preserve_exact_size = True
                    if nxt is not cur:
                        try:
                            cur.close()
                        except Exception:
                            pass
                    cur = nxt

            # 4) Upscale (skip for exact pixel outputs)
            if not preserve_exact_size:
                nxt = upscale_image_pil(cur, min_width, min_height, auto_upscale)
                if nxt is not cur:
                    try:
                        cur.close()
                    except Exception:
                        pass
                cur = nxt

            # 5) Size optimize
            buf, _fmt, _ct = optimize_image_for_size(
                cur, preserve_exact_size=preserve_exact_size
            )
            return buf.getvalue()
        finally:
            try:
                cur.close()
            except Exception:
                pass

    with Image.open(io.BytesIO(img_bytes)) as img:
        w, h = img.size
        half_w = w // 2
        half_h = h // 2

        boxes = [
            (0, 0, half_w, half_h),
            (half_w, 0, w, half_h),
            (0, half_h, half_w, h),
            (half_w, half_h, w, h),
        ]

        results: list[bytes] = []
        for box in boxes:
            quad = img.crop(box)
            results.append(_process_one(quad))
        return results


def is_likely_midjourney_collage(img_bytes: bytes) -> bool:
    """Heuristic: Midjourney 2x2 grids are large square images."""
    try:
        with Image.open(io.BytesIO(img_bytes)) as img:
            w, h = img.size
            return w == h and w >= 1500
    except Exception:
        return False
