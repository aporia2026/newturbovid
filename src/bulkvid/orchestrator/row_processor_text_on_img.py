"""Text-on-image row processor — one IMAGE from the user's manual image
with the operator-typed text overlaid in the center. No video.

The full pipeline (article fetch / script / TTS / Rendi / ZapCap) was
stripped on 2026-06-09 per the user's "should produce an image, not a
video" call. The tab now writes back a still PNG URL to the
"Ready Image" column.

Pipeline:

  1. Validate the manual image URL
  2. Download manual image
  3. Pillow: blurred-bg fit into target aspect + center-overlay text
  4. Upload composed PNG to storage
  5. Return the image URL

Pure CPU + 2 network hops. Runs the Pillow rendering via
``asyncio.to_thread`` so the event loop stays free.

The TextOnImgRow dataclass still carries ``article_url`` / ``voice_over``
/ ``zapcap`` / ``script_pattern`` / ``open_comments`` for wire
compatibility with the existing Apps Script payload — those fields are
ignored here. Apps Script still has those columns on the sheet so the
operator can configure them, but they have no effect on the output.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from bulkvid.adapters.rendi import normalize_aspect_ratio
from bulkvid.logging import get_logger, set_context
from bulkvid.models.row import (
    STATUS_IMAGE_DOWNLOAD_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_STORAGE_FAILED,
    STATUS_SUCCESS,
    RowResult,
    TextOnImgRow,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.pipeline.text_overlay import overlay_text_on_image_bytes

_log = get_logger("row")


async def _download(url: str, *, timeout: float = 60.0) -> bytes:
    async with httpx.AsyncClient(timeout=timeout) as c:
        resp = await c.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content


def _slug_segment(value: str, *, fallback: str = "na") -> str:
    """Normalize a free-text segment for use inside a storage object key:
    lowercase, collapse runs of non-alphanumerics into a single ``-``, trim
    leading/trailing ``-``. Empty/whitespace input yields ``fallback`` so we
    never produce a name with ``__`` runs from blank cells."""
    out = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return out or fallback


def _country_code(value: str, *, fallback: str = "NA") -> str:
    """Country codes are conventionally uppercase ISO-style (``DE``, ``MX``).
    Keep them uppercase in the filename for at-a-glance recognition."""
    out = re.sub(r"[^A-Z0-9]+", "", (value or "").upper())
    return out or fallback


def _size_slug(aspect_ratio: str) -> str:
    """``9:16`` → ``9x16``. ``:`` is unfriendly in URLs and filenames; ``x`` is
    the conventional separator for pixel sizes. Falls back through
    ``normalize_aspect_ratio`` so weird inputs (``09:16``, ``WxH`` pixel
    inputs, ``auto``) all land on a known-good string first."""
    return normalize_aspect_ratio(aspect_ratio).replace(":", "x")


def _image_object_key(row: TextOnImgRow, *, now: datetime | None = None) -> str:
    """Build a readable, sortable storage key for the composed PNG.

    Shape: ``bulkvid/text_on_img/{COUNTRY}_{vertical}_{date}_{size}_r{row}_{6hex}.png``.
    Each segment is bounded:
      * country: 2-4 char ISO-style code, uppercase
      * vertical: slugified, capped at 40 chars (operators occasionally
        paste long taglines into the column)
      * date: UTC ``YYYY-MM-DD`` so dates sort lexicographically
      * size: aspect ratio with ``:`` replaced by ``x``
      * row: sheet row number — gives inter-row uniqueness within a batch
      * 6 hex: ``uuid4().hex[:6]`` — prevents collisions on same-row
        regenerations in the same UTC day (operator might re-run, parallel
        in-flight jobs, etc.).
    """
    n = now or datetime.now(timezone.utc)
    country = _country_code(row.country)
    vertical = _slug_segment(row.vertical, fallback="general")[:40]
    date_part = n.strftime("%Y-%m-%d")
    size = _size_slug(row.aspect_ratio)
    short = uuid.uuid4().hex[:6]
    fname = f"{country}_{vertical}_{date_part}_{size}_r{row.row_num}_{short}.png"
    return f"bulkvid/text_on_img/{fname}"


def _is_valid_http_url(url: str) -> bool:
    return isinstance(url, str) and url.strip().startswith(("http://", "https://"))


@dataclass
class _Costs:
    storage: float = 0.0

    @property
    def total(self) -> float:
        return round(self.storage, 6)


async def process_text_on_img_row(
    row: TextOnImgRow,
    clients: PipelineClients,
    *,
    job_id: str | None = None,
) -> RowResult:
    """Run the text-on-img IMAGE pipeline for one row. Returns a RowResult.
    Never raises."""
    set_context(batch_id=job_id, row_num=row.row_num)
    t0 = time.monotonic()
    costs = _Costs()
    object_key = _image_object_key(row)
    metadata: dict[str, Any] = {
        "row_num": row.row_num,
        "country": row.country,
        "vertical": row.vertical,
        "aspect_ratio": row.aspect_ratio,
        "tab": "text_on_img",
        "overlay_chars": len(row.text or ""),
    }

    _log.info(
        "row_start",
        country=row.country,
        vertical=row.vertical,
        aspect=row.aspect_ratio,
        tab="text_on_img",
        overlay_chars=len(row.text or ""),
    )

    if not _is_valid_http_url(row.manual_image_url):
        return _fail(
            row, STATUS_IMAGE_DOWNLOAD_FAILED,
            "manual_image_url is missing or not an HTTP(S) URL",
            t0, costs, metadata,
        )

    try:
        # ─── Stage 1: download manual image ───
        try:
            source_image_bytes = await _download(row.manual_image_url, timeout=60.0)
        except Exception as e:
            # httpx errors sometimes stringify to "" (e.g. ConnectError on
            # a TLS handshake failure). Include the exception class AND the
            # URL host so the sidebar surfaces an actionable cause.
            url_host = (row.manual_image_url or "")[:80]
            err_str = str(e) or repr(e) or type(e).__name__
            return _fail(
                row, STATUS_IMAGE_DOWNLOAD_FAILED,
                f"manual image download failed ({type(e).__name__}): "
                f"{err_str} — url={url_host}",
                t0, costs, metadata,
            )

        # ─── Stage 2: compose (blurred-bg fit + text overlay) ───
        try:
            # Pillow rendering is CPU-bound — offload so the event loop stays
            # free to advance other rows during the overlay paint.
            composed_bytes = await asyncio.to_thread(
                overlay_text_on_image_bytes,
                source_image_bytes,
                row.text,
                aspect_ratio=row.aspect_ratio,
            )
        except Exception as e:
            _log.exception("text_overlay_failed", error=str(e)[:200])
            return _fail(
                row, STATUS_INTERNAL_ERROR,
                f"text overlay failed: {e!s}",
                t0, costs, metadata,
            )

        # ─── Stage 3: upload composed PNG ───
        try:
            composed_upload = await clients.storage.upload_bytes(
                composed_bytes,
                key=object_key,
                content_type="image/png",
            )
            costs.storage += composed_upload.cost_usd
            final_url = composed_upload.url
            metadata["composed_image_bytes"] = len(composed_bytes)
        except Exception as e:
            return _fail(row, STATUS_STORAGE_FAILED, str(e), t0, costs, metadata)

        return _ok(row, [final_url], t0, costs, metadata)

    except Exception as e:
        _log.exception("row_internal_error", error=str(e))
        return _fail(row, STATUS_INTERNAL_ERROR, str(e), t0, costs, metadata)


# ── Result builders ──────────────────────────────────────────────────────────


def _ok(
    row: TextOnImgRow,
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
    # writer writes each entry into ready_video_start + slot regardless of
    # whether it's a video or image URL. Keeping the field name avoids
    # touching the sheet-writer + queue paths.
    return RowResult(
        row_num=row.row_num,
        status=status,
        video_urls=image_urls,
        cost_usd=costs.total,
        elapsed_seconds=elapsed,
        metadata=metadata,
    )


def _fail(
    row: TextOnImgRow,
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
