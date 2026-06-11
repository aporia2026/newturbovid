"""Resilient HTTP image download — retry + browser UA for flaky CDNs.

Operator-provided URLs in the Manual-image flow frequently come from
Facebook's ad redirect endpoint (``facebook.com/ads/image/?d=...``),
Instagram's CDN, or Pinterest — all known to flake on TLS handshake
under load, and to refuse requests carrying the default ``python-httpx``
User-Agent. A single hiccup kills the row outright.

This helper wraps ``httpx.AsyncClient.get`` with:

  - A current Chrome stable User-Agent (FB/IG/Pinterest expect this).
  - Exponential backoff retry on transient network/TLS errors.
  - Retried 5xx server errors; permanent 4xx errors are NOT retried.
  - A clear ``ImageDownloadError`` carrying the host + last reason so
    a row-failure message reads ``download from facebook.com failed
    after 3 attempts: SSLError: ...`` instead of bare ``_ssl.c:1010``.

Plan: ``_plans/2026-06-11-resilient-image-download.md``.
"""

from __future__ import annotations

import asyncio
import ssl
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


# Network/TLS errors we retry. SSL handshakes flake on FB/IG endpoints
# under load; connect/read errors are similar transient blips.
_RETRYABLE_NETWORK_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteError,
    ssl.SSLError,
)


def _host_of(url: str) -> str:
    try:
        return urlparse(url).hostname or "?"
    except Exception:
        return "?"


async def download_image(
    url: str,
    *,
    timeout: float = 60.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
    user_agent: str = DEFAULT_USER_AGENT,
) -> bytes:
    """GET ``url`` and return the response bytes. Retries transient failures.

    Retries:
      - ``httpx.ConnectError`` / ``ConnectTimeout`` / ``ReadError`` /
        ``ReadTimeout`` / ``RemoteProtocolError`` / ``WriteError``
      - ``ssl.SSLError`` (the FB ``/ads/image/?d=...`` failure mode)
      - 5xx server responses

    Does NOT retry:
      - 4xx client responses (permanent — bad URL, forbidden, gone)

    Raises:
        ImageDownloadError: All retries exhausted, or hit a hard 4xx.
            Message names the host and the last failure reason; the full
            URL is deliberately NOT logged so encoded query blobs with
            personalization tokens (FB ``?d=...``) do not leak into logs.

    See ``_plans/2026-06-11-resilient-image-download.md``.
    """
    host = _host_of(url)
    headers = {"User-Agent": user_agent}
    last_err: str = ""
    backoff = _INITIAL_BACKOFF_SECONDS

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, headers=headers
    ) as client:
        for attempt in range(1, max_retries + 1):
            try:
                resp = await client.get(url)
                if 400 <= resp.status_code < 500:
                    # Permanent — don't retry. Surface status to the operator.
                    raise ImageDownloadError(
                        f"download from {host} failed: HTTP {resp.status_code} "
                        f"(client error — not retried)"
                    )
                resp.raise_for_status()
                if attempt > 1:
                    _log.info(
                        "image_download_recovered",
                        host=host,
                        attempt=attempt,
                    )
                return resp.content
            except ImageDownloadError:
                raise
            except _RETRYABLE_NETWORK_EXCEPTIONS as e:
                last_err = f"{type(e).__name__}: {e}"
            except httpx.HTTPStatusError as e:
                # 5xx — retry. (4xx was already short-circuited above.)
                last_err = f"HTTP {e.response.status_code}"

            if attempt < max_retries:
                _log.warning(
                    "image_download_retry",
                    host=host,
                    attempt=attempt,
                    max_attempts=max_retries,
                    error=last_err[:200],
                )
                await asyncio.sleep(backoff)
                backoff *= _BACKOFF_FACTOR

    raise ImageDownloadError(
        f"download from {host} failed after {max_retries} attempts: {last_err}"
    )
