"""Probe a remote image for its native (width, height) without decoding pixels.

Used by the "blank Change Size → use the manual image as-is" path: the row
processor calls ``probe_native_dimensions(url)`` before deciding the output
aspect ratio. When blank, the probed ``(w, h)`` is folded back into
``row.aspect_ratio`` as ``"WxH"``, which the downstream Rendi adapter
(``dimensions_for_ratio``) already accepts and passes through verbatim.

Never raises. Returns ``None`` on any failure (network, decode, etc.) so
callers can fall back gracefully without crashing the row.

Plan: ``_plans/2026-06-14-blank-size-uses-native-image.md`` §D.3.
"""

from __future__ import annotations

import io
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

from bulkvid.http_download import download_image
from bulkvid.logging import get_logger

_log = get_logger("image")


async def probe_native_dimensions(
    url: str,
    *,
    timeout: float = 30.0,
) -> tuple[int, int] | None:
    """Download ``url`` and return ``(width, height)`` from the image header.

    Returns ``None`` on any failure (network, malformed bytes, unknown
    format). ``Image.open`` reads just the header for ``.size`` — no pixel
    decode — so this is cheap and decompression-bomb resistant.

    The URL's query string is NOT logged (FB ``?d=...`` tokens carry
    personalization data); only the host is. Matches the redaction
    convention in ``http_download.py``.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    host = _host_of(url)
    try:
        data = await download_image(url, timeout=timeout)
    except Exception as e:
        _log.warning(
            "probe_download_failed",
            host=host,
            err=f"{type(e).__name__}: {str(e)[:160]}",
        )
        return None

    try:
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
    except (UnidentifiedImageError, OSError, ValueError) as e:
        _log.warning(
            "probe_decode_failed",
            host=host,
            bytes=len(data),
            err=f"{type(e).__name__}: {str(e)[:160]}",
        )
        return None

    if w <= 0 or h <= 0:
        # PIL has been observed to return (0, 0) on truncated bytes. Treat as
        # a probe failure so the caller falls back to the default ratio.
        _log.warning("probe_zero_dimensions", host=host, bytes=len(data))
        return None
    return (int(w), int(h))


def _host_of(url: str) -> str:
    try:
        return urlparse(url).hostname or "?"
    except Exception:
        return "?"
