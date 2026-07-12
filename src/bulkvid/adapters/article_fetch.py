"""Article fetch adapter — ScrapingBee → direct-HTTP fallback.

The bulk pipeline needs the *full* article body, not just the title, to
generate copy / a voiceover script in the article's language. ScrapingBee
renders the page with JS on and returns the HTML, which we strip to text. When
ScrapingBee is unavailable — no key configured, or its account is down — a free
direct-HTTP fetch is the last resort: pull the page ourselves and pull its
paragraph text. It's lower quality (and datacenter IPs are sometimes blocked),
but a row that ships beats a row that fails outright.

Tavily was removed 2026-07-12 (account disabled for non-payment, and it billed
every failed attempt). ScrapingBee is the sole paid extractor now. Plan:
``_plans/2026-07-12-upscale-resilience-tavily-removal.md``.

Cost note: ScrapingBee bills only on a successful fetch (~$0.003). The direct
fetch is free.

Plan: ``_plans/2026-06-02-aporia-bulk-video-tool.md`` §5 (Article fetch), §11;
direct fallback ``_plans/2026-07-08-motion-ads-tab.md`` (provider-down hardening).
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
COST_SCRAPINGBEE_REQUEST_USD = 0.003


# ── Endpoints ────────────────────────────────────────────────────────────────
SCRAPINGBEE_BASE_URL = "https://app.scrapingbee.com/api/v1/"


# ── Errors ───────────────────────────────────────────────────────────────────


class ArticleFetchError(RuntimeError):
    """All fetch strategies exhausted."""


class ScrapingBeeError(RuntimeError):
    """ScrapingBee returned an error (used internally; not propagated)."""


# ── Result ───────────────────────────────────────────────────────────────────


@dataclass
class ArticleResult:
    url: str
    content: str
    source: str                       # "scrapingbee" | "direct"
    char_count: int
    cost_usd: float


# ── HTML → text ──────────────────────────────────────────────────────────────


_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_PARAGRAPH_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.DOTALL | re.IGNORECASE)

# A <p> block shorter than this is almost always chrome (a nav label, a button,
# a copyright line), not article prose — skip it when extracting main text.
_MIN_PARAGRAPH_CHARS = 40


def html_to_text(html: str) -> str:
    """Strip HTML to plain text. Best-effort; good enough as a fallback path."""
    h = _SCRIPT_RE.sub(" ", html)
    h = _STYLE_RE.sub(" ", h)
    h = _TAG_RE.sub(" ", h)
    h = html_lib.unescape(h)
    return _WHITESPACE_RE.sub(" ", h).strip()


def extract_main_text(html: str) -> str:
    """Best-effort ARTICLE text from a raw page, no dependencies.

    Pulls the text of every ``<p>`` block long enough to be prose, which skips
    most of a page's nav / menu / footer chrome (those live in ``<a>`` / ``<li>``
    / ``<nav>``, not ``<p>``). Falls back to a whole-page strip when paragraph
    extraction finds too little — some sites render body text without ``<p>``
    wrappers. Used only by the direct-HTTP fallback, where we get raw HTML
    instead of a provider's pre-extracted text.
    """
    paragraphs: list[str] = []
    for raw in _PARAGRAPH_RE.findall(html):
        text = html_to_text(raw)
        if len(text) >= _MIN_PARAGRAPH_CHARS:
            paragraphs.append(text)
    joined = "\n\n".join(paragraphs).strip()
    if len(joined) < 200:    # paragraph extraction found little — strip it all
        return html_to_text(html)
    return joined


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    # Cut at a word boundary if possible.
    cut = text.rfind(" ", 0, max_chars)
    return text[: cut if cut > 0 else max_chars]


# ── Fetcher ──────────────────────────────────────────────────────────────────


class ArticleFetcher:
    """Article fetcher with ScrapingBee → free direct-HTTP fallback."""

    def __init__(
        self,
        scrapingbee_api_key: str = "",
        max_chars: int = 50_000,
        scrapingbee_timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not scrapingbee_api_key:
            raise ValueError("ArticleFetcher requires scrapingbee_api_key")
        self._scrapingbee_key = scrapingbee_api_key
        self._max_chars = max_chars
        self._scrapingbee_timeout = scrapingbee_timeout
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=scrapingbee_timeout)

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def __aenter__(self) -> ArticleFetcher:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

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

    # ── Direct-HTTP path (free last resort) ─────────────────────────────

    async def _fetch_direct(self, url: str) -> str:
        """Fetch the page ourselves with a browser UA and pull its main text.

        No paid API — reached only when ScrapingBee is unavailable. Lower
        quality (and datacenter IPs are sometimes blocked with a 403), so it is
        deliberately last.
        """
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en,*;q=0.5",
        }
        _log.info("article_direct_submit", url=url[:200])
        resp = await self._client.get(
            url,
            headers=headers,
            timeout=self._scrapingbee_timeout,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            raise ArticleFetchError(
                f"direct HTTP {resp.status_code} for {url}"
            )
        text = extract_main_text(resp.text)
        if not text:
            raise ArticleFetchError(f"direct fetch returned empty body for {url}")
        return text

    # ── Public entrypoint ───────────────────────────────────────────────

    async def fetch(self, url: str) -> ArticleResult:
        """Fetch full article content. ScrapingBee first, direct-HTTP fallback."""
        if not url or not url.startswith(("http://", "https://")):
            raise ArticleFetchError(f"Invalid URL: {url!r}")

        cost = 0.0

        # Primary: ScrapingBee (JS-rendered, HTML stripped to text).
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

        # Last resort: free direct-HTTP fetch. Reached when ScrapingBee is
        # down/unconfigured. No added cost. A lower-quality body that lets the
        # row ship beats failing the row outright.
        try:
            content = await self._fetch_direct(url)
            truncated = _truncate(content, self._max_chars)
            _log.info(
                "article_fetch_ok",
                url=url[:200],
                source="direct",
                chars=len(truncated),
                cost_usd=cost,
            )
            return ArticleResult(
                url=url,
                content=truncated,
                source="direct",
                char_count=len(truncated),
                cost_usd=cost,
            )
        except (ArticleFetchError, httpx.HTTPError, asyncio.TimeoutError) as e:
            _log.error(
                "article_direct_failed",
                url=url[:200],
                error=str(e)[:200],
            )

        raise ArticleFetchError(
            f"All article fetch strategies failed for {url}"
        )


def build_fetcher_from_settings(settings: Settings | None = None) -> ArticleFetcher:
    s = settings or get_settings()
    if not s.SCRAPINGBEE_API_KEY:
        raise ValueError("Need SCRAPINGBEE_API_KEY")
    return ArticleFetcher(
        scrapingbee_api_key=s.SCRAPINGBEE_API_KEY,
        max_chars=s.ARTICLE_MAX_CONTENT_CHARS,
        scrapingbee_timeout=s.SCRAPINGBEE_TIMEOUT_SECONDS,
    )
