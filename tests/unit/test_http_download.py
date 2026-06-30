"""Tests for the resilient image download helper.

Two backends are exercised:

  * **httpx** (default) — non-FB/IG hosts. Covers retry/backoff on
    transient TLS + network errors, 4xx short-circuit, exhausted-retry
    messaging, and the browser User-Agent header.
  * **curl_cffi impersonate** — FB/IG hosts. Covers host routing,
    impersonate happy/retry/4xx behavior, and graceful degradation
    when curl_cffi is not available on the host platform.

Plans:
  - ``_plans/2026-06-11-resilient-image-download.md`` (initial helper)
  - ``_plans/2026-06-30-facebook-tls-fingerprint.md`` (curl_cffi route)
"""

from __future__ import annotations

import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from bulkvid.http_download import (
    DEFAULT_USER_AGENT,
    ImageDownloadError,
    _should_impersonate,
    download_image,
)

# Non-impersonate host — generic CDN, routes through httpx. Used for the
# bulk of the test suite so respx can mock the network call directly.
GENERIC_URL = "https://cdn.example.com/image.jpg"
GENERIC_HOST = "cdn.example.com"

# Impersonate host — Facebook ad redirect, routes through curl_cffi. Used
# for the impersonate-backend tests with curl_cffi mocked.
FB_URL = "https://www.facebook.com/ads/image/?d=AQICTest"
FB_HOST = "www.facebook.com"


# ── Host routing ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host,expected",
    [
        ("www.facebook.com", True),
        ("facebook.com", True),
        ("scontent.fbcdn.net", True),
        ("fbcdn.net", True),
        ("www.instagram.com", True),
        ("scontent.cdninstagram.com", True),
        ("WWW.FACEBOOK.COM", True),  # case-insensitive
        ("cdn.example.com", False),
        ("storage.googleapis.com", False),
        ("api.rendi.dev", False),
        ("facebook.com.evil.com", False),  # suffix must be a real subdomain boundary
        ("", False),
    ],
)
def test_should_impersonate_matches_fb_ig_hosts(host: str, expected: bool) -> None:
    assert _should_impersonate(host) is expected


# ── httpx backend: happy path ────────────────────────────────────────────────


@respx.mock
async def test_download_image_returns_bytes_on_success() -> None:
    respx.get(GENERIC_URL).mock(return_value=httpx.Response(200, content=b"PNGDATA"))
    out = await download_image(GENERIC_URL)
    assert out == b"PNGDATA"


@respx.mock
async def test_download_image_sends_browser_user_agent_on_httpx_path() -> None:
    """Non-FB/IG CDNs may still inspect UA — keep the browser-like header."""
    captured: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, content=b"OK")

    respx.get(GENERIC_URL).mock(side_effect=_handler)
    await download_image(GENERIC_URL)
    assert captured == [DEFAULT_USER_AGENT]
    assert "Chrome" in captured[0]
    assert "Mozilla" in captured[0]


# ── httpx backend: retry on transient network/TLS errors ─────────────────────


@respx.mock
async def test_download_image_retries_on_ssl_error_then_succeeds() -> None:
    """First attempt hits an SSL handshake failure; second attempt succeeds."""
    call_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ssl.SSLError("[SSL] unknown error (_ssl.c:1010)")
        return httpx.Response(200, content=b"recovered")

    respx.get(GENERIC_URL).mock(side_effect=_handler)
    with patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()):
        out = await download_image(GENERIC_URL, max_retries=3)
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

    respx.get(GENERIC_URL).mock(side_effect=_handler)
    with patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()):
        out = await download_image(GENERIC_URL, max_retries=3)
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

    respx.get(GENERIC_URL).mock(side_effect=_handler)
    with patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()):
        out = await download_image(GENERIC_URL, max_retries=3)
    assert out == b"after-5xx"
    assert call_count["n"] == 2


# ── httpx backend: non-retryable cases ───────────────────────────────────────


@respx.mock
async def test_download_image_does_not_retry_on_404() -> None:
    """4xx is permanent — retrying wastes batch latency."""
    call_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(404, content=b"not found")

    respx.get(GENERIC_URL).mock(side_effect=_handler)
    with patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        with pytest.raises(ImageDownloadError) as exc_info:
            await download_image(GENERIC_URL, max_retries=3)

    assert call_count["n"] == 1
    sleep_mock.assert_not_awaited()
    assert "404" in str(exc_info.value)
    assert GENERIC_HOST in str(exc_info.value)


@respx.mock
async def test_download_image_does_not_retry_on_403() -> None:
    respx.get(GENERIC_URL).mock(return_value=httpx.Response(403))
    with patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(ImageDownloadError) as exc_info:
            await download_image(GENERIC_URL, max_retries=3)
    assert "403" in str(exc_info.value)


# ── httpx backend: exhausted retries ─────────────────────────────────────────


@respx.mock
async def test_download_image_raises_after_exhausting_retries_on_ssl_error() -> None:
    """When the CDN is genuinely down, the row gets a CLEAR error
    (host + last failure reason), not a bare ``_ssl.c:1010`` line."""

    def _handler(request: httpx.Request) -> httpx.Response:
        raise ssl.SSLError("[SSL] unknown error (_ssl.c:1010)")

    respx.get(GENERIC_URL).mock(side_effect=_handler)
    with patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(ImageDownloadError) as exc_info:
            await download_image(GENERIC_URL, max_retries=3)

    msg = str(exc_info.value)
    assert GENERIC_HOST in msg
    assert "3 attempts" in msg
    assert "SSLError" in msg


@respx.mock
async def test_download_image_max_retries_one_means_single_attempt() -> None:
    """``max_retries=1`` honored — no retry, single attempt then raise."""
    call_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        raise httpx.ConnectError("nope")

    respx.get(GENERIC_URL).mock(side_effect=_handler)
    with patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        with pytest.raises(ImageDownloadError):
            await download_image(GENERIC_URL, max_retries=1)

    assert call_count["n"] == 1
    sleep_mock.assert_not_awaited()


# ── curl_cffi impersonate backend ────────────────────────────────────────────
#
# These tests build a fake ``curl_cffi.requests`` module out of ``MagicMock``
# so they run regardless of whether the real wheel is installed on the dev
# machine. The shape we mock matches the surface ``_get_via_impersonate``
# actually touches: ``AsyncSession`` async-context-manager + ``.get()`` +
# ``exceptions.ConnectionError / SSLError / ConnectTimeout / ReadTimeout /
# Timeout / RequestException``.


def _make_fake_curl_cffi(get_side_effect):
    """Build a stand-in for ``curl_cffi.requests`` with ``get_side_effect``
    determining what ``AsyncSession().get()`` does on each call."""

    class _SSLError(Exception): ...

    class _ConnectionError(Exception): ...

    class _ConnectTimeout(Exception): ...

    class _ReadTimeout(Exception): ...

    class _Timeout(Exception): ...

    class _RequestException(Exception): ...

    exc_mod = MagicMock()
    exc_mod.SSLError = _SSLError
    exc_mod.ConnectionError = _ConnectionError
    exc_mod.ConnectTimeout = _ConnectTimeout
    exc_mod.ReadTimeout = _ReadTimeout
    exc_mod.Timeout = _Timeout
    exc_mod.RequestException = _RequestException

    session = MagicMock()
    session.get = AsyncMock(side_effect=get_side_effect)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    requests_mod = MagicMock()
    requests_mod.exceptions = exc_mod
    requests_mod.AsyncSession = MagicMock(return_value=session)
    return requests_mod, exc_mod, session


async def test_impersonate_backend_returns_bytes_for_fb_host() -> None:
    """FB host routes through curl_cffi and returns the response body."""
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"FBIMG"
    fake_mod, _exc, session = _make_fake_curl_cffi(get_side_effect=[resp])

    with (
        patch("bulkvid.http_download._IMPERSONATE_AVAILABLE", True),
        patch("bulkvid.http_download._curl_requests", fake_mod),
    ):
        out = await download_image(FB_URL)

    assert out == b"FBIMG"
    # Confirm the call actually went through curl_cffi, not httpx.
    session.get.assert_awaited_once_with(FB_URL)
    fake_mod.AsyncSession.assert_called_once()
    # Chrome impersonation must be requested — that is the whole point.
    kwargs = fake_mod.AsyncSession.call_args.kwargs
    assert kwargs.get("impersonate") == "chrome"


async def test_impersonate_backend_retries_on_ssl_error_then_succeeds() -> None:
    """Transient TLS failure on FB still recovers across retries."""
    resp_ok = MagicMock()
    resp_ok.status_code = 200
    resp_ok.content = b"recovered-fb"

    # Build the fake first to grab the SSLError class, then wire side_effect.
    fake_mod, exc_mod, session = _make_fake_curl_cffi(get_side_effect=None)
    session.get.side_effect = [exc_mod.SSLError("handshake failed"), resp_ok]

    with (
        patch("bulkvid.http_download._IMPERSONATE_AVAILABLE", True),
        patch("bulkvid.http_download._curl_requests", fake_mod),
        patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()),
    ):
        out = await download_image(FB_URL, max_retries=3)

    assert out == b"recovered-fb"
    assert session.get.await_count == 2


async def test_impersonate_backend_retries_on_connect_timeout_then_succeeds() -> None:
    resp_ok = MagicMock()
    resp_ok.status_code = 200
    resp_ok.content = b"second-fb"

    fake_mod, exc_mod, session = _make_fake_curl_cffi(get_side_effect=None)
    session.get.side_effect = [exc_mod.ConnectTimeout("connect timed out"), resp_ok]

    with (
        patch("bulkvid.http_download._IMPERSONATE_AVAILABLE", True),
        patch("bulkvid.http_download._curl_requests", fake_mod),
        patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()),
    ):
        out = await download_image(FB_URL, max_retries=3)

    assert out == b"second-fb"
    assert session.get.await_count == 2


async def test_impersonate_backend_does_not_retry_on_4xx() -> None:
    """4xx is permanent on the impersonate path too — single attempt, raise."""
    resp = MagicMock()
    resp.status_code = 403
    resp.content = b"forbidden"
    fake_mod, _exc, session = _make_fake_curl_cffi(get_side_effect=[resp])

    with (
        patch("bulkvid.http_download._IMPERSONATE_AVAILABLE", True),
        patch("bulkvid.http_download._curl_requests", fake_mod),
        patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()) as sleep_mock,
    ):
        with pytest.raises(ImageDownloadError) as exc_info:
            await download_image(FB_URL, max_retries=3)

    assert session.get.await_count == 1
    sleep_mock.assert_not_awaited()
    assert "403" in str(exc_info.value)
    assert FB_HOST in str(exc_info.value)


async def test_impersonate_backend_raises_after_exhausting_retries() -> None:
    fake_mod, exc_mod, session = _make_fake_curl_cffi(get_side_effect=None)
    session.get.side_effect = exc_mod.SSLError("handshake failed")

    with (
        patch("bulkvid.http_download._IMPERSONATE_AVAILABLE", True),
        patch("bulkvid.http_download._curl_requests", fake_mod),
        patch("bulkvid.http_download.asyncio.sleep", new=AsyncMock()),
    ):
        with pytest.raises(ImageDownloadError) as exc_info:
            await download_image(FB_URL, max_retries=3)

    msg = str(exc_info.value)
    assert FB_HOST in msg
    assert "3 attempts" in msg
    assert "SSLError" in msg


# ── Graceful degradation: curl_cffi unavailable ──────────────────────────────


@respx.mock
async def test_fb_url_falls_back_to_httpx_when_curl_cffi_unavailable() -> None:
    """If curl_cffi failed to import on this host, FB URLs go via httpx —
    same behavior as before the impersonate route was added. No crash."""
    respx.get(FB_URL).mock(return_value=httpx.Response(200, content=b"httpx-fallback"))
    with patch("bulkvid.http_download._IMPERSONATE_AVAILABLE", False):
        out = await download_image(FB_URL)
    assert out == b"httpx-fallback"
