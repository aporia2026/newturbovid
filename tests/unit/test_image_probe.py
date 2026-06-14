"""Tests for the native-image probe helper.

The probe is load-bearing for the "blank Change Size = use the manual image
as-is" path: the row processor relies on it to translate a remote URL into
``(width, height)`` so the downstream Rendi / kie adapters receive the
operator's intended size instead of silently falling back to 9:16.

Plan: ``_plans/2026-06-14-blank-size-uses-native-image.md`` §D.3.
"""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from PIL import Image

from bulkvid.http_download import ImageDownloadError
from bulkvid.image_probe import probe_native_dimensions

GOOD_URL = "https://example.com/ad.png"


def _png_bytes(width: int, height: int) -> bytes:
    """Build a tiny in-memory PNG with the requested dimensions."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


async def test_probe_returns_dimensions_on_success() -> None:
    """Happy path: a real PNG round-trips to ``(width, height)``."""
    expected = (1080, 1350)
    with patch(
        "bulkvid.image_probe.download_image",
        return_value=_png_bytes(*expected),
    ):
        out = await probe_native_dimensions(GOOD_URL)
    assert out == expected


async def test_probe_returns_none_on_download_failure() -> None:
    """ImageDownloadError from the http helper is swallowed → ``None``."""
    with patch(
        "bulkvid.image_probe.download_image",
        side_effect=ImageDownloadError("CDN timeout"),
    ):
        out = await probe_native_dimensions(GOOD_URL)
    assert out is None


async def test_probe_returns_none_on_unknown_network_error() -> None:
    """Any other exception from the http helper is also swallowed (the
    probe never blocks a row)."""
    with patch(
        "bulkvid.image_probe.download_image",
        side_effect=RuntimeError("unexpected"),
    ):
        out = await probe_native_dimensions(GOOD_URL)
    assert out is None


async def test_probe_returns_none_on_garbage_bytes() -> None:
    """PIL refusing to decode the bytes → ``None`` (logged, not raised)."""
    with patch(
        "bulkvid.image_probe.download_image",
        return_value=b"not an image",
    ):
        out = await probe_native_dimensions(GOOD_URL)
    assert out is None


@pytest.mark.parametrize("blank_url", ["", "   ", None])
async def test_probe_returns_none_for_blank_url_without_fetching(blank_url) -> None:
    """A blank URL short-circuits without calling ``download_image``. Prevents
    a wasted retry against ``""`` (which would 4xx out)."""
    with patch("bulkvid.image_probe.download_image") as mock_dl:
        out = await probe_native_dimensions(blank_url)
    assert out is None
    assert mock_dl.call_count == 0
