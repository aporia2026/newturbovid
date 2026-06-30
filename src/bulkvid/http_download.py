"""Resilient HTTP image download — retry + browser TLS impersonation.

Operator-provided URLs in the Manual-image flow frequently come from
Facebook's ad redirect endpoint (``facebook.com/ads/image/?d=...``),
Instagram's CDN, or Pinterest. Two distinct failure modes are in play:

1. **Transient TLS / network flake** — FB/IG sometimes reset the
   handshake under load. Retry + browser User-Agent recovers it.
2. **TLS fingerprint rejection** — FB's edge inspects the ClientHello
   (JA3, ALPN, extension order) and refuses Python's ``ssl`` defaults
   *regardless of the UA header*. No amount of retrying fixes this —
   only an actual browser-shaped handshake gets through.

For (2) we route FB/IG hosts through ``curl_cffi``'s ``AsyncSession``
with ``impersonate="chrome"``. Everything else (Rendi.dev results, GCS,
S3, generic CDNs) stays on ``httpx`` so no other download path can
regress. If ``curl_cffi`` cannot import for any reason, FB downloads
fall back to httpx and we log the degradation once at startup.

This helper wraps both backends in a single retry loop:

  - A current Chrome stable User-Agent on the httpx path.
  - Exponential backoff retry on transient network/TLS errors.
  - Retried 5xx server errors; permanent 4xx errors are NOT retried.
  - A clear ``ImageDownloadError`` carrying the host + last reason so
    a row-failure message reads ``download from facebook.com failed
    after 3 attempts: SSLError: ...`` instead of bare ``_ssl.c:1010``.

Plans:
  - ``_plans/2026-06-11-resilient-image-download.md`` (initial helper)
  - ``_plans/2026-06-30-facebook-tls-fingerprint.md`` (curl_cffi route)
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any
from urllib.parse import urlparse

import httpx

from bulkvid.logging import get_logger

_log = get_logger("http")


# Pinned to a current Chrome stable string. The CDNs we hit key off the
# major version + platform tokens, not the minor/patch; bump roughly
# every few months when Chrome's stable channel drifts far enough.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_MAX_RETRIES = 3
_INITIAL_BACKOFF_SECONDS = 0.5
_BACKOFF_FACTOR = 2.0


class ImageDownloadError(RuntimeError):
    """Image download failed after exhausting retries (or hit a hard 4xx)."""


class _Retryable(Exception):
    """Signal from a backend that this attempt failed transiently."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ── Backend availability ─────────────────────────────────────────────────────
#
# curl_cffi is imported lazily here. If the wheel cannot load on the host
# platform (unsupported ABI, corrupt install), we degrade to the httpx path
# for FB/IG hosts as well — same behavior as the pre-curl_cffi code, no harder
# failure than today.

_curl_requests: Any = None
_IMPERSONATE_AVAILABLE: bool = False
_curl_import_error: str = ""

try:
    from curl_cffi import requests as _curl_requests_mod

    _curl_requests = _curl_requests_mod
    _IMPERSONATE_AVAILABLE = True
except Exception as _e:  # pragma: no cover - exercised only on platform mismatch
    _curl_import_error = f"{type(_e).__name__}: {_e}"

# Emit one startup line so a missing wheel shows up in prod logs immediately,
# not on the first failing FB URL.
_log.info(
    "image_download_backend",
    impersonate_available=_IMPERSONATE_AVAILABLE,
    import_error=_curl_import_error[:200],
)


# Hosts where Python's stock TLS handshake gets dropped at the edge. Anything
# matching one of these suffixes is routed through curl_cffi's impersonate
# backend; everything else stays on httpx so non-FB downloads cannot regress.
_IMPERSONATE_HOST_SUFFIXES: tuple[str, ...] = (
    "facebook.com",
    "fbcdn.net",
    "instagram.com",
    "cdninstagram.com",
)


def _host_of(url: str) -> str:
    try:
        return urlparse(url).hostname or "?"
    except Exception:
        return "?"


def _should_impersonate(host: str) -> bool:
    """True when ``host`` (or its parent domain) requires browser TLS."""
    if not host:
        return False
    h = host.lower()
    return any(h == s or h.endswith("." + s) for s in _IMPERSONATE_HOST_SUFFIXES)


# Network/TLS errors we retry on the httpx path. SSL handshakes flake on
# FB/IG endpoints under load; connect/read errors are similar transient blips.
_RETRYABLE_HTTPX_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteError,
    ssl.SSLError,
)


# ── Per-backend one-shot GETs ────────────────────────────────────────────────
#
# Each backend either returns response bytes, raises ImageDownloadError for
# permanent (4xx) failures, or raises _Retryable for transient failures. The
# shared retry loop below handles backoff, logging, and final exhaustion.


async def _get_via_httpx(
    url: str,
    *,
    host: str,
    timeout: float,
    user_agent: str,
) -> bytes:
    headers = {"User-Agent": user_agent}
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=headers
        ) as client:
            resp = await client.get(url)
    except _RETRYABLE_HTTPX_EXCEPTIONS as e:
        raise _Retryable(f"{type(e).__name__}: {e}") from e

    if 400 <= resp.status_code < 500:
        raise ImageDownloadError(
            f"download from {host} failed: HTTP {resp.status_code} "
            f"(client error — not retried)"
        )
    if resp.status_code >= 500:
        raise _Retryable(f"HTTP {resp.status_code}")
    return resp.content


async def _get_via_impersonate(
    url: str,
    *,
    host: str,
    timeout: float,
) -> bytes:
    """GET via curl_cffi with Chrome TLS impersonation. FB/IG only."""
    assert _curl_requests is not None  # gated by _IMPERSONATE_AVAILABLE at call site

    # curl_cffi's exception hierarchy. We import inside the function so a
    # degraded build (no curl_cffi) doesn't blow up at module load.
    exc_mod = _curl_requests.exceptions
    retryable = (
        exc_mod.ConnectionError,
        exc_mod.ConnectTimeout,
        exc_mod.ReadTimeout,
        exc_mod.Timeout,
        exc_mod.SSLError,
    )

    try:
        async with _curl_requests.AsyncSession(
            timeout=timeout,
            impersonate="chrome",
            verify=True,
            allow_redirects=True,
        ) as session:
            resp = await session.get(url)
    except retryable as e:
        raise _Retryable(f"{type(e).__name__}: {e}") from e
    except exc_mod.RequestException as e:
        # Any other curl_cffi error (DNS, proxy, decode). Treat as retryable —
        # cheaper to burn two more attempts than to fail a row on a one-off.
        raise _Retryable(f"{type(e).__name__}: {e}") from e

    status = int(resp.status_code)
    if 400 <= status < 500:
        raise ImageDownloadError(
            f"download from {host} failed: HTTP {status} "
            f"(client error — not retried)"
        )
    if status >= 500:
        raise _Retryable(f"HTTP {status}")
    return bytes(resp.content)


# ── Public entry point ───────────────────────────────────────────────────────


async def download_image(
    url: str,
    *,
    timeout: float = 60.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
    user_agent: str = DEFAULT_USER_AGENT,
) -> bytes:
    """GET ``url`` and return the response bytes. Retries transient failures.

    Routing:
      - FB / IG hosts → ``curl_cffi`` with ``impersonate="chrome"`` (real
        Chrome TLS ClientHello). Falls back to httpx if curl_cffi failed
        to import on this host.
      - Everything else → ``httpx`` with a browser User-Agent header.

    Retries (both backends):
      - Connection / TLS / read errors (per-backend exception families).
      - 5xx server responses.
      - 4xx is NOT retried (permanent client error — bad URL, forbidden, gone).

    Raises:
        ImageDownloadError: All retries exhausted, or hit a hard 4xx.
            Message names the host and the last failure reason; the full
            URL is deliberately NOT logged so encoded query blobs with
            personalization tokens (FB ``?d=...``) do not leak into logs.

    See:
      - ``_plans/2026-06-11-resilient-image-download.md``
      - ``_plans/2026-06-30-facebook-tls-fingerprint.md``
    """
    host = _host_of(url)
    use_impersonate = _IMPERSONATE_AVAILABLE and _should_impersonate(host)
    backend_name = "impersonate" if use_impersonate else "httpx"

    _log.debug("image_download_route", host=host, backend=backend_name)

    last_err = ""
    backoff = _INITIAL_BACKOFF_SECONDS

    for attempt in range(1, max_retries + 1):
        try:
            if use_impersonate:
                data = await _get_via_impersonate(url, host=host, timeout=timeout)
            else:
                data = await _get_via_httpx(
                    url, host=host, timeout=timeout, user_agent=user_agent
                )
        except ImageDownloadError:
            # Permanent 4xx — never retry.
            raise
        except _Retryable as e:
            last_err = e.reason
        else:
            if attempt > 1:
                _log.info(
                    "image_download_recovered",
                    host=host,
                    attempt=attempt,
                    backend=backend_name,
                )
            return data

        if attempt < max_retries:
            _log.warning(
                "image_download_retry",
                host=host,
                attempt=attempt,
                max_attempts=max_retries,
                backend=backend_name,
                error=last_err[:200],
            )
            await asyncio.sleep(backoff)
            backoff *= _BACKOFF_FACTOR

    raise ImageDownloadError(
        f"download from {host} failed after {max_retries} attempts: {last_err}"
    )
