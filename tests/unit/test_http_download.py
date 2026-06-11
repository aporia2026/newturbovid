"""Tests for the resilient image download helper.

Covers retry/backoff behavior on transient TLS + network errors (the
FB ``/ads/image/?d=...`` SSL handshake failure mode), permanent-error
short-circuit on 4xx, exhausted-retry messaging, and the browser
User-Agent header.

Plan: ``_plans/2026-06-11-resilient-image-download.md``.
"""

from __future__ import annotations

import ssl
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from bulkvid.http_download import (
    DEFAULT_USER_AGENT,
    ImageDownloadError,
    download_image,
)

GOOD_URL = "https://www.facebook.com/ads/image/?d=AQICTest"
GOOD_HOST = "www.facebook.com"


# ── Happy path ───────────────────────────────────────────────────────────────


@respx.mock
async def test_download_image_returns_bytes_on_success() -> None:
    respx.get(GOOD_URL).mock(return_value=httpx.Response(200, content=b"PNGDATA"))
    out = await download_image(GOOD_URL)
    assert out == b"PNGDATA"


@respx.mock
async def test_download_image_sends_browser_user_agent() -> None:
    """FB/IG CDNs refuse the default httpx UA — operators rely on browser-like UA."""
    captured: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, content=b"OK")

    respx.get(GOOD_URL).mock(side_effect=_handler)
    await download_image(GOOD_URL)
    assert captured == [DEFAULT_USER_AGENT]
    assert "Chrome" in captured[0]
    assert "Mozilla" in captured[0]


# ── Retry on transient network/TLS errors ────────────────────────────────────


@respx.mock
async def test_download_image_retries_on_ssl_error_then_succeeds() -> None:
    """The reported FB failure mode — ``[SSL] unknown error (_ssl.c:1010)``.

    First attempt hits an SSL handshake failure; helper retries and the
    second attempt succeeds. Row processor gets bytes, not a dead row.
    """
    call_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ssl.SSLError("[SSL] unknown error (_ssl.c:1010)")
        return httpx.Response(200, content=b"recovered")

    respx.get(GOOD_URL).mock(side_effect=_handler)
    with patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()):
        out = await download_image(GOOD_URL, max_retries=3)
    assert out == b"recovered"
    assert call_count["n"] == 2


@respx.mock
async def test_download_image_retries_on_connect_error_then_succeeds() -> None:
    call_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, content=b"second-try")

    respx.get(GOOD_URL).mock(side_effect=_handler)
    with patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()):
        out = await download_image(GOOD_URL, max_retries=3)
    assert out == b"second-try"
    assert call_count["n"] == 2


@respx.mock
async def test_download_image_retries_on_5xx_then_succeeds() -> None:
    """Server-side transient — retried, NOT bubbled up like 4xx."""
    call_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(502, content=b"bad gateway")
        return httpx.Response(200, content=b"after-5xx")

    respx.get(GOOD_URL).mock(side_effect=_handler)
    with patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()):
        out = await download_image(GOOD_URL, max_retries=3)
    assert out == b"after-5xx"
    assert call_count["n"] == 2


# ── Non-retryable cases ──────────────────────────────────────────────────────


@respx.mock
async def test_download_image_does_not_retry_on_404() -> None:
    """4xx is permanent — retrying wastes batch latency."""
    call_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(404, content=b"not found")

    respx.get(GOOD_URL).mock(side_effect=_handler)
    with patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        with pytest.raises(ImageDownloadError) as exc_info:
            await download_image(GOOD_URL, max_retries=3)

    assert call_count["n"] == 1
    sleep_mock.assert_not_awaited()
    assert "404" in str(exc_info.value)
    assert GOOD_HOST in str(exc_info.value)


@respx.mock
async def test_download_image_does_not_retry_on_403() -> None:
    respx.get(GOOD_URL).mock(return_value=httpx.Response(403))
    with patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(ImageDownloadError) as exc_info:
            await download_image(GOOD_URL, max_retries=3)
    assert "403" in str(exc_info.value)


# ── Exhausted retries ────────────────────────────────────────────────────────


@respx.mock
async def test_download_image_raises_after_exhausting_retries_on_ssl_error() -> None:
    """When the FB CDN is genuinely down, the row gets a CLEAR error
    (host + last failure reason), not a bare ``_ssl.c:1010`` line."""

    def _handler(request: httpx.Request) -> httpx.Response:
        raise ssl.SSLError("[SSL] unknown error (_ssl.c:1010)")

    respx.get(GOOD_URL).mock(side_effect=_handler)
    with patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(ImageDownloadError) as exc_info:
            await download_image(GOOD_URL, max_retries=3)

    msg = str(exc_info.value)
    assert GOOD_HOST in msg
    assert "3 attempts" in msg
    assert "SSLError" in msg


@respx.mock
async def test_download_image_max_retries_one_means_single_attempt() -> None:
    """``max_retries=1`` honored — no retry, single attempt then raise."""
    call_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        raise httpx.ConnectError("nope")

    respx.get(GOOD_URL).mock(side_effect=_handler)
    with patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        with pytest.raises(ImageDownloadError):
            await download_image(GOOD_URL, max_retries=1)

    assert call_count["n"] == 1
    sleep_mock.assert_not_awaited()
