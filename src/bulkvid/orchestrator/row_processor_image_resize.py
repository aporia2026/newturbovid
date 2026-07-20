"""image_resize row processor — one IMAGE reframed to a new aspect ratio.

The image_resize tab takes the operator's Manual Image (col D) and the target
size (col H, "Change Size") and produces a single image at the new aspect ratio
that looks native there — Nano Banana 2 extends the background/design to fill the
new frame and re-renders any baked-in text, so nothing important is cropped. The
reframed image URL is written back to the "Ready Image" column (col K). No video.

Pipeline:

  1. Validate the manual image URL and the target size (blank size → soft error;
     a resize with no target size is meaningless and we won't pay to regenerate
     an image at its own ratio).
  2. Reframe with ``edit_with_fallback`` (Nano Banana 2 → GPT Image 2 → Atlas)
     at the target ratio, 2K, with a prompt that forbids changing the text.
  3. Download the result (kie URLs are ephemeral).
  4. If the operator asked for exact pixels (``WxH``), crop+resize to that exact
     size; otherwise cap the model's ratio output under 2 MB.
  5. Upload to our storage and return the stable URL.

The ``text`` / ``article_url`` / ``voice_over`` / ``zapcap`` / ``script_pattern``
/ ``open_comments`` fields on :class:`ImageResizeRow` are carried for Apps Script
payload compatibility but ignored here — the text this tab preserves already
lives in the image pixels.

Plan: ``_plans/2026-07-20-image-resize-tab.md``.
"""

from __future__ import annotations

import asyncio
import io
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from PIL import Image

from bulkvid.adapters.rendi import normalize_aspect_ratio
from bulkvid.http_download import download_image
from bulkvid.image_ops import (
    CutSpec,
    crop_to_ratio_pil,
    optimize_image_for_size,
    parse_cut_dimension,
)
from bulkvid.logging import get_logger, set_context
from bulkvid.models.row import (
    STATUS_IMAGE_DOWNLOAD_FAILED,
    STATUS_IMAGE_GEN_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_STORAGE_FAILED,
    STATUS_SUCCESS,
    ImageResizeRow,
    RowResult,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.pipeline.image_gen import edit_with_fallback

_log = get_logger("row")


# ── Tunables ─────────────────────────────────────────────────────────────────

# 2K over 1K ($0.06 vs $0.04/image): the extra resolution keeps re-rendered
# marketing text legible, which is the whole point of this tab.
RESIZE_RESOLUTION = "2K"

# The reframe instruction. The text-preservation clauses are the main defense
# against Nano Banana nudging a DE umlaut / IT accent when it regenerates the
# frame (see memory ``localization-quality-attention``). We deliberately forbid
# adding anything (memory ``no-real-brands-in-generated-images``): this is a
# faithful reframe, not a redesign.
REFRAME_PROMPT = (
    "Reframe this image to the requested aspect ratio. Keep the main subject, "
    "composition, colors, lighting, and overall design exactly as they are. "
    "Naturally extend the existing background to fill the new frame so nothing "
    "important is cropped and the result looks like it was originally created at "
    "this size. Preserve every piece of text in the image exactly: same words, "
    "same language, same spelling, same font style, same color, same position "
    "relative to the subject. Do not translate, rewrite, add, or remove any "
    "text, and keep all text crisp and legible. Do not add logos, watermarks, "
    "captions, borders, or any new elements."
)


def _slug_segment(value: str, *, fallback: str = "na") -> str:
    """Normalize a free-text segment for a storage object key: lowercase,
    collapse runs of non-alphanumerics into a single ``-``, trim edges. Empty
    input yields ``fallback`` so we never emit ``__`` runs from blank cells."""
    out = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return out or fallback


def _country_code(value: str, *, fallback: str = "NA") -> str:
    """Country codes are conventionally uppercase ISO-style (``DE``, ``IT``)."""
    out = re.sub(r"[^A-Z0-9]+", "", (value or "").upper())
    return out or fallback


def _size_slug(aspect_ratio: str) -> str:
    """``9:16`` → ``9x16``. ``:`` is unfriendly in URLs/filenames. Falls back
    through ``normalize_aspect_ratio`` so odd inputs land on a known-good
    string first."""
    return normalize_aspect_ratio(aspect_ratio).replace(":", "x")


def _image_object_key(
    row: ImageResizeRow, *, ext: str = "png", now: datetime | None = None
) -> str:
    """Readable, sortable storage key for the reframed image.

    Shape: ``bulkvid/image_resize/{COUNTRY}_{vertical}_resize_{date}_{size}_r{row}_{6hex}.{ext}``.
    Mirrors the text_on_img key layout (country / vertical / marker / UTC date /
    size / sheet row / 6 hex for same-row re-run uniqueness). The extension
    follows the optimizer's chosen format so the filename never lies about its
    bytes (the 2 MB optimizer downgrades a large frame to JPEG)."""
    n = now or datetime.now(timezone.utc)
    country = _country_code(row.country)
    vertical = _slug_segment(row.vertical, fallback="general")[:40]
    date_part = n.strftime("%Y-%m-%d")
    size = _size_slug(row.aspect_ratio)
    short = uuid.uuid4().hex[:6]
    fname = f"{country}_{vertical}_resize_{date_part}_{size}_r{row.row_num}_{short}.{ext}"
    return f"bulkvid/image_resize/{fname}"


def _is_valid_http_url(url: str) -> bool:
    return isinstance(url, str) and url.strip().startswith(("http://", "https://"))


def _reframe_to_bytes(raw: bytes, cut: CutSpec) -> tuple[bytes, str]:
    """Post-process the model output on the calling thread's executor.

    For a pixel target (``WxH``), crop to that exact ratio then resize to the
    exact pixels (``preserve_exact_size`` so the optimizer only drops quality,
    never geometry). For a ratio target, Nano Banana already produced the right
    aspect, so we just cap it under 2 MB. Returns ``(bytes, content_type)``.
    """
    with Image.open(io.BytesIO(raw)) as img:
        img.load()
        preserve_exact = cut.type == "pixels" and bool(cut.width) and bool(cut.height)
        cur: Image.Image = img
        if preserve_exact:
            assert cut.width is not None and cut.height is not None
            ratio = cut.width / cut.height
            fitted = crop_to_ratio_pil(cur, ratio)
            try:
                resized = fitted.resize(
                    (cut.width, cut.height), Image.Resampling.LANCZOS
                )
            finally:
                if fitted is not cur:
                    fitted.close()
            cur = resized
        try:
            buf, _fmt, content_type = optimize_image_for_size(
                cur, preserve_exact_size=preserve_exact
            )
            return buf.getvalue(), content_type
        finally:
            if cur is not img:
                cur.close()


@dataclass
class _Costs:
    image_gen: float = 0.0
    storage: float = 0.0

    @property
    def total(self) -> float:
        return round(self.image_gen + self.storage, 6)


async def process_image_resize_row(
    row: ImageResizeRow,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
) -> RowResult:
    """Run the image_resize IMAGE pipeline for one row. Returns a RowResult.
    Never raises."""
    set_context(batch_id=job_id, row_num=row.row_num)
    t0 = time.monotonic()
    costs = _Costs()
    metadata: dict[str, Any] = {
        "row_num": row.row_num,
        "country": row.country,
        "vertical": row.vertical,
        "aspect_ratio": row.aspect_ratio,
        "tab": "image_resize",
    }

    _log.info(
        "row_start",
        country=row.country,
        vertical=row.vertical,
        aspect=row.aspect_ratio,
        tab="image_resize",
    )

    if not _is_valid_http_url(row.manual_image_url):
        return _fail(
            row, STATUS_IMAGE_DOWNLOAD_FAILED,
            "Manual Image is missing or not an HTTP(S) URL",
            t0, costs, metadata,
        )

    # A resize with no target is a no-op we'd still pay for — tell the operator
    # to pick a size instead of silently regenerating the image at its own ratio.
    cut = parse_cut_dimension(row.aspect_ratio)
    if cut.type == "none":
        return _fail(
            row, STATUS_INTERNAL_ERROR,
            "Change Size is required — pick a target size like 9:16 or "
            "1080x1920 in column H",
            t0, costs, metadata,
        )

    # Nano Banana takes a "W:H" ratio; normalize a pixel input (WxH) to the
    # nearest valid ratio for the model, then crop to exact pixels afterwards.
    model_aspect = normalize_aspect_ratio(row.aspect_ratio)
    metadata["target_type"] = cut.type
    metadata["model_aspect"] = model_aspect

    try:
        # ─── Stage 1: reframe to the new aspect ratio ───
        try:
            reframed_url, gen_cost = await edit_with_fallback(
                kie=clients.kie,
                atlas=clients.atlas,
                source_image_url=row.manual_image_url,
                prompt=REFRAME_PROMPT,
                aspect_ratio=model_aspect,
                resolution=RESIZE_RESOLUTION,
            )
            costs.image_gen += gen_cost
        except Exception as e:
            return _fail(
                row, STATUS_IMAGE_GEN_FAILED,
                f"image reframe failed: {e!s}",
                t0, costs, metadata,
            )

        # ─── Stage 2: download the reframed image (kie URLs are ephemeral) ───
        try:
            reframed_bytes = await download_image(reframed_url, timeout=120.0)
        except Exception as e:
            url_host = (reframed_url or "")[:80]
            err_str = str(e) or repr(e) or type(e).__name__
            return _fail(
                row, STATUS_IMAGE_DOWNLOAD_FAILED,
                f"reframed image download failed ({type(e).__name__}): "
                f"{err_str} — url={url_host}",
                t0, costs, metadata,
            )

        # ─── Stage 3: fit to exact pixels (if asked) + cap size ───
        try:
            # Pillow work is CPU-bound — offload so the event loop keeps
            # advancing other rows during the crop/optimize.
            final_bytes, content_type = await asyncio.to_thread(
                _reframe_to_bytes, reframed_bytes, cut
            )
            metadata["final_image_bytes"] = len(final_bytes)
        except Exception as e:
            _log.exception("image_resize_postprocess_failed", error=str(e)[:200])
            return _fail(
                row, STATUS_INTERNAL_ERROR,
                f"image post-processing failed: {e!s}",
                t0, costs, metadata,
            )

        # ─── Stage 4: upload to our storage ───
        ext = "jpg" if content_type == "image/jpeg" else "png"
        try:
            upload = await clients.storage.upload_bytes(
                final_bytes,
                key=_image_object_key(row, ext=ext),
                content_type=content_type,
            )
            costs.storage += upload.cost_usd
            final_url = upload.url
        except Exception as e:
            return _fail(row, STATUS_STORAGE_FAILED, str(e), t0, costs, metadata)

        return _ok(row, [final_url], t0, costs, metadata)

    except Exception as e:
        _log.exception("row_internal_error", error=str(e))
        return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)


# ── Result builders ──────────────────────────────────────────────────────────


def _ok(
    row: ImageResizeRow,
    image_urls: list[str],
    t0: float,
    costs: _Costs,
    metadata: dict[str, Any],
    *,
    status: str = STATUS_SUCCESS,
) -> RowResult:
    elapsed = round(time.monotonic() - t0, 3)
    metadata["cost_breakdown"] = costs.__dict__.copy()
    _log.info(
        "row_done",
        status=status,
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
        image_count=len(image_urls),
    )
    # RowResult.video_urls is the generic "Ready cell URL list" — the sheet
    # writer writes each entry into the ready column regardless of whether it's
    # a video or image URL (same as text_on_img).
    return RowResult(
        row_num=row.row_num,
        status=status,
        video_urls=image_urls,
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
        metadata=metadata,
    )


def _fail(
    row: ImageResizeRow,
    status: str,
    error: str,
    t0: float,
    costs: _Costs,
    metadata: dict[str, Any],
) -> RowResult:
    elapsed = round(time.monotonic() - t0, 3)
    metadata["cost_breakdown"] = costs.__dict__.copy()
    _log.error(
        "row_failed",
        status=status,
        error=error[:300],
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
    )
    return RowResult(
        row_num=row.row_num,
        status=status,
        video_urls=[],
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
        error=error[:1000],
        metadata=metadata,
    )
