"""Article fetch adapter — Tavily primary, ScrapingBee fallback.

The bulk pipeline needs the *full* article body, not just the title, to
generate a ~10-second voiceover script in the article's language. Tavily's
``/extract`` endpoint gives us clean text directly. When Tavily fails (paywall,
cookie wall, JS-only sites), we fall back to ScrapingBee with JS rendering on
and strip the resulting HTML.

Cost note: most calls cost Tavily-only (~$0.008). ScrapingBee fires only on
fallback (~$0.003).

Plan: ``_plans/2026-06-02-aporia-bulk-video-tool.md`` §5 (Article fetch), §11.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import re
from dataclasses import dataclass
from typing import Any

import httpx

from bulkvid.config import Settings, get_settings
from bulkvid.logging import get_logger

_log = get_logger("article")


# ── Pricing (USD) ────────────────────────────────────────────────────────────
# Verified plan §11 2026-06-02. Refresh before each release.
COST_TAVILY_EXTRACT_USD = 0.008
COST_SCRAPINGBEE_REQUEST_USD = 0.003


# ── Endpoints ────────────────────────────────────────────────────────────────
TAVILY_BASE_URL = "https://api.tavily.com"
SCRAPINGBEE_BASE_URL = "https://app.scrapingbee.com/api/v1/"


# ── Errors ───────────────────────────────────────────────────────────────────


class ArticleFetchError(RuntimeError):
    """All fetch strategies exhausted."""


class TavilyError(RuntimeError):
    """Tavily returned an error (used internally; not propagated)."""


class ScrapingBeeError(RuntimeError):
    """ScrapingBee returned an error (used internally; not propagated)."""


# ── Result ───────────────────────────────────────────────────────────────────


@dataclass
class ArticleResult:
    url: str
    content: str
    source: str                       # "tavily" | "scrapingbee"
    char_count: int
    cost_usd: float


# ── HTML → text ──────────────────────────────────────────────────────────────


_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def html_to_text(html: str) -> str:
    """Strip HTML to plain text. Best-effort; good enough as a fallback path."""
    h = _SCRIPT_RE.sub(" ", html)
    h = _STYLE_RE.sub(" ", h)
    h = _TAG_RE.sub(" ", h)
    h = html_lib.unescape(h)
    return _WHITESPACE_RE.sub(" ", h).strip()


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    # Cut at a word boundary if possible.
    cut = text.rfind(" ", 0, max_chars)
    return text[: cut if cut > 0 else max_chars]


# ── Fetcher ──────────────────────────────────────────────────────────────────


class ArticleFetcher:
    """Two-stage article fetcher with Tavily → ScrapingBee fallback."""

    def __init__(
        self,
        tavily_api_key: str = "",
        scrapingbee_api_key: str = "",
        max_chars: int = 50_000,
        tavily_timeout: float = 15.0,
        scrapingbee_timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not tavily_api_key and not scrapingbee_api_key:
            raise ValueError(
                "ArticleFetcher requires at least one of "
                "tavily_api_key or scrapingbee_api_key"
            )
        self._tavily_key = tavily_api_key
        self._scrapingbee_key = scrapingbee_api_key
        self._max_chars = max_chars
        self._tavily_timeout = tavily_timeout
        self._scrapingbee_timeout = scrapingbee_timeout
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=max(tavily_timeout, scrapingbee_timeout))

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def __aenter__(self) -> ArticleFetcher:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    # ── Tavily path ─────────────────────────────────────────────────────

    async def _fetch_tavily(self, url: str) -> str:
        endpoint = f"{TAVILY_BASE_URL}/extract"
        headers = {
            "Authorization": f"Bearer {self._tavily_key}",
            "Content-Type": "application/json",
        }
        payload = {"urls": [url], "extract_depth": "advanced"}
        _log.info("article_tavily_submit", url=url[:200])
        resp = await self._client.post(
            endpoint, json=payload, headers=headers, timeout=self._tavily_timeout
        )
        if resp.status_code != 200:
            raise TavilyError(
                f"Tavily HTTP {resp.status_code}: {resp.text[:200]}"
            )
        body = resp.json()
        results = body.get("results") or []
        if not results:
            raise TavilyError(f"Tavily returned no results for {url}")
        # raw_content is the canonical full article body in newer Tavily versions;
        # `content` is the older shape.
        first = results[0]
        content = (first.get("raw_content") or first.get("content") or "").strip()
        if not content:
            raise TavilyError(f"Tavily result had empty content for {url}")
        return content

    # ── ScrapingBee path ────────────────────────────────────────────────

    async def _fetch_scrapingbee(self, url: str) -> str:
        params = {
            "api_key": self._scrapingbee_key,
            "url": url,
            "render_js": "true",
            "block_resources": "true",
        }
        _log.info("article_scrapingbee_submit", url=url[:200])
        resp = await self._client.get(
            SCRAPINGBEE_BASE_URL,
            params=params,
            timeout=self._scrapingbee_timeout,
        )
        if resp.status_code != 200:
            raise ScrapingBeeError(
                f"ScrapingBee HTTP {resp.status_code}: {resp.text[:200]}"
            )
        text = html_to_text(resp.text)
        if not text:
            raise ScrapingBeeError(f"ScrapingBee returned empty body for {url}")
        return text

    # ── Public entrypoint ───────────────────────────────────────────────

    async def fetch(self, url: str) -> ArticleResult:
        """Fetch full article content. Tavily first, ScrapingBee fallback."""
        if not url or not url.startswith(("http://", "https://")):
            raise ArticleFetchError(f"Invalid URL: {url!r}")

        cost = 0.0

        # Try Tavily.
        if self._tavily_key:
            try:
                content = await self._fetch_tavily(url)
                cost += COST_TAVILY_EXTRACT_USD
                truncated = _truncate(content, self._max_chars)
                _log.info(
                    "article_fetch_ok",
                    url=url[:200],
                    source="tavily",
                    chars=len(truncated),
                    cost_usd=cost,
                )
                return ArticleResult(
                    url=url,
                    content=truncated,
                    source="tavily",
                    char_count=len(truncated),
                    cost_usd=cost,
                )
            except (TavilyError, httpx.HTTPError, asyncio.TimeoutError) as e:
                _log.warning(
                    "article_tavily_failed",
                    url=url[:200],
                    error=str(e)[:200],
                )
                cost += COST_TAVILY_EXTRACT_USD  # Tavily still bills failed attempts

        # Fallback to ScrapingBee.
        if self._scrapingbee_key:
            try:
                content = await self._fetch_scrapingbee(url)
                cost += COST_SCRAPINGBEE_REQUEST_USD
                truncated = _truncate(content, self._max_chars)
                _log.info(
                    "article_fetch_ok",
                    url=url[:200],
                    source="scrapingbee",
                    chars=len(truncated),
                    cost_usd=cost,
                )
                return ArticleResult(
                    url=url,
                    content=truncated,
                    source="scrapingbee",
                    char_count=len(truncated),
                    cost_usd=cost,
                )
            except (ScrapingBeeError, httpx.HTTPError, asyncio.TimeoutError) as e:
                _log.error(
                    "article_scrapingbee_failed",
                    url=url[:200],
                    error=str(e)[:200],
                )

        raise ArticleFetchError(
            f"All article fetch strategies failed for {url}"
        )


def build_fetcher_from_settings(settings: Settings | None = None) -> ArticleFetcher:
    s = settings or get_settings()
    if not s.TAVILY_API_KEY and not s.SCRAPINGBEE_API_KEY:
        raise ValueError(
            "Need at least one of TAVILY_API_KEY or SCRAPINGBEE_API_KEY"
        )
    return ArticleFetcher(
        tavily_api_key=s.TAVILY_API_KEY,
        scrapingbee_api_key=s.SCRAPINGBEE_API_KEY,
        max_chars=s.ARTICLE_MAX_CONTENT_CHARS,
        tavily_timeout=s.TAVILY_TIMEOUT_SECONDS,
        scrapingbee_timeout=s.SCRAPINGBEE_TIMEOUT_SECONDS,
    )
