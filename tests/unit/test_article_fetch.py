"""Tests for the article-fetch adapter.

All network calls mocked via respx. Covers:
  - html_to_text: strips script/style/tags, decodes entities, collapses whitespace
  - _truncate: respects word boundaries, no-op when under limit
  - Tavily success path (Bearer header, /extract endpoint)
  - Tavily error -> ScrapingBee fallback succeeds (with HTML stripped)
  - Both fail -> ArticleFetchError
  - Invalid URL -> ArticleFetchError without any network call
  - max_chars truncation
  - Constructor rejects when both keys empty
"""

from __future__ import annotations

import httpx
import pytest
import respx

from bulkvid.adapters.article_fetch import (
    COST_SCRAPINGBEE_REQUEST_USD,
    COST_TAVILY_EXTRACT_USD,
    SCRAPINGBEE_BASE_URL,
    TAVILY_BASE_URL,
    ArticleFetcher,
    ArticleFetchError,
    _truncate,
    extract_main_text,
    html_to_text,
)


# ── html_to_text ────────────────────────────────────────────────────────────


def test_html_to_text_strips_tags() -> None:
    html = "<p>Hello <strong>world</strong></p>"
    assert html_to_text(html) == "Hello world"


def test_html_to_text_strips_script_and_style() -> None:
    html = """
    <html>
      <head><style>body { color: red; }</style></head>
      <body>
        <script>alert('xss')</script>
        <p>Visible content</p>
      </body>
    </html>
    """
    out = html_to_text(html)
    assert "Visible content" in out
    assert "alert" not in out
    assert "color: red" not in out


def test_html_to_text_decodes_entities() -> None:
    html = "<p>caf&eacute; &amp; tea</p>"
    assert html_to_text(html) == "café & tea"


def test_html_to_text_collapses_whitespace() -> None:
    html = "<p>line\n\n\n  one</p>\n\n<p>line   two</p>"
    out = html_to_text(html)
    assert out == "line one line two"


# ── extract_main_text ───────────────────────────────────────────────────────


def test_extract_main_text_keeps_prose_drops_chrome() -> None:
    # The prose paragraph is well over the 200-char paragraph-path threshold, so
    # extraction uses the paragraphs and never falls back to a whole-page strip.
    prose = (
        "This paragraph is comfortably long enough to be treated as genuine "
        "article prose and kept by the extractor, well past the minimum length "
        "the heuristic uses to separate real body copy from short navigation "
        "labels, buttons, and boilerplate that clutter a page's chrome."
    )
    html = (
        "<nav><a>Home</a></nav>"
        "<p>Short</p>"    # under the min length — dropped as chrome
        f"<p>{prose}</p>"
        "<footer><p>tiny</p></footer>"
    )
    out = extract_main_text(html)
    assert "article prose and kept" in out
    assert "Home" not in out
    assert "Short" not in out    # too short to survive the prose filter


def test_extract_main_text_falls_back_to_full_strip_without_paragraphs() -> None:
    # No <p> tags at all -> fall back to a whole-page strip so we still get text.
    html = "<div>" + ("Body text without paragraph wrappers. " * 10) + "</div>"
    out = extract_main_text(html)
    assert "Body text without paragraph wrappers." in out


# ── _truncate ───────────────────────────────────────────────────────────────


def test_truncate_no_op_when_under_limit() -> None:
    assert _truncate("short", 100) == "short"


def test_truncate_cuts_at_word_boundary() -> None:
    text = "one two three four five six seven"
    out = _truncate(text, 20)
    assert len(out) <= 20
    # Should not end mid-word; last char should not be a letter that was split.
    assert not out.endswith("fou")
    assert " " not in out[-1]  # no trailing whitespace


def test_truncate_hard_cut_when_no_space() -> None:
    # Single huge word, no spaces — falls back to hard cut at max_chars.
    out = _truncate("a" * 1000, 50)
    assert len(out) == 50


# ── Constructor ─────────────────────────────────────────────────────────────


def test_constructor_rejects_when_both_keys_empty() -> None:
    with pytest.raises(ValueError):
        ArticleFetcher(tavily_api_key="", scrapingbee_api_key="")


# ── Tavily happy path ──────────────────────────────────────────────────────


@respx.mock
async def test_tavily_success_returns_article() -> None:
    respx.post(f"{TAVILY_BASE_URL}/extract").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.com/a",
                        "raw_content": "Full article body about a topic.",
                    }
                ]
            },
        )
    )
    async with ArticleFetcher(tavily_api_key="tav_test_key") as fetcher:
        result = await fetcher.fetch("https://example.com/a")
    assert result.source == "tavily"
    assert "Full article body" in result.content
    assert result.cost_usd == COST_TAVILY_EXTRACT_USD


@respx.mock
async def test_tavily_uses_bearer_auth() -> None:
    captured: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("authorization", ""))
        return httpx.Response(
            200,
            json={"results": [{"raw_content": "x"}]},
        )

    respx.post(f"{TAVILY_BASE_URL}/extract").mock(side_effect=_handler)
    async with ArticleFetcher(tavily_api_key="tav_key") as fetcher:
        await fetcher.fetch("https://example.com/a")
    assert captured == ["Bearer tav_key"]


@respx.mock
async def test_tavily_legacy_content_field_also_accepted() -> None:
    # Some Tavily versions return ``content`` instead of ``raw_content``.
    respx.post(f"{TAVILY_BASE_URL}/extract").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"content": "Older shape article."}]},
        )
    )
    async with ArticleFetcher(tavily_api_key="tav") as fetcher:
        result = await fetcher.fetch("https://example.com/x")
    assert "Older shape article." in result.content


# ── Tavily fail -> ScrapingBee fallback ─────────────────────────────────────


@respx.mock
async def test_tavily_failure_falls_back_to_scrapingbee() -> None:
    respx.post(f"{TAVILY_BASE_URL}/extract").mock(
        return_value=httpx.Response(500, text="server error")
    )
    respx.get(SCRAPINGBEE_BASE_URL).mock(
        return_value=httpx.Response(
            200,
            text="<html><body><p>Real article content.</p></body></html>",
        )
    )
    async with ArticleFetcher(
        tavily_api_key="tav", scrapingbee_api_key="sb"
    ) as fetcher:
        result = await fetcher.fetch("https://example.com/blocked")
    assert result.source == "scrapingbee"
    assert "Real article content." in result.content
    # Cost charged for BOTH attempts (Tavily attempt + ScrapingBee fallback).
    assert result.cost_usd == COST_TAVILY_EXTRACT_USD + COST_SCRAPINGBEE_REQUEST_USD


@respx.mock
async def test_only_scrapingbee_configured_skips_tavily() -> None:
    respx.get(SCRAPINGBEE_BASE_URL).mock(
        return_value=httpx.Response(
            200, text="<p>ScrapingBee-only content</p>"
        )
    )
    async with ArticleFetcher(scrapingbee_api_key="sb") as fetcher:
        result = await fetcher.fetch("https://example.com/a")
    assert result.source == "scrapingbee"
    assert result.cost_usd == COST_SCRAPINGBEE_REQUEST_USD


# ── Both fail ───────────────────────────────────────────────────────────────


@respx.mock
async def test_all_three_fail_raises_article_fetch_error() -> None:
    # Tavily 500, ScrapingBee 500, AND the free direct-HTTP last resort 500.
    respx.post(f"{TAVILY_BASE_URL}/extract").mock(
        return_value=httpx.Response(500)
    )
    respx.get(SCRAPINGBEE_BASE_URL).mock(return_value=httpx.Response(500))
    respx.get("https://example.com/dead").mock(return_value=httpx.Response(500))
    async with ArticleFetcher(
        tavily_api_key="tav", scrapingbee_api_key="sb"
    ) as fetcher:
        with pytest.raises(ArticleFetchError):
            await fetcher.fetch("https://example.com/dead")


@respx.mock
async def test_both_providers_fail_direct_fallback_succeeds() -> None:
    # Tavily + ScrapingBee both down (e.g. Tavily account disabled, no working
    # ScrapingBee) -> the free direct fetch pulls the page's paragraphs.
    respx.post(f"{TAVILY_BASE_URL}/extract").mock(return_value=httpx.Response(402))
    respx.get(SCRAPINGBEE_BASE_URL).mock(return_value=httpx.Response(500))
    page = (
        "<html><body><nav><a>Home</a><a>About</a></nav>"
        "<p>This is the real article body paragraph, long enough to count as "
        "genuine prose rather than a navigation label.</p>"
        "<p>A second substantial paragraph continues the article with more "
        "detail so the extractor keeps it too.</p>"
        "<footer><p>© 2026</p></footer></body></html>"
    )
    respx.get("https://example.com/story").mock(
        return_value=httpx.Response(200, text=page)
    )
    async with ArticleFetcher(
        tavily_api_key="tav", scrapingbee_api_key="sb"
    ) as fetcher:
        result = await fetcher.fetch("https://example.com/story")
    assert result.source == "direct"
    assert "real article body paragraph" in result.content
    assert "second substantial paragraph" in result.content
    assert "Home" not in result.content and "© 2026" not in result.content
    # Direct fetch is free; ScrapingBee bills only on SUCCESS, so a 500 there
    # adds nothing — only Tavily's always-billed attempt is charged.
    assert result.cost_usd == COST_TAVILY_EXTRACT_USD


@respx.mock
async def test_direct_fallback_when_no_provider_keys_configured() -> None:
    # A deploy with a (present-but-dead) Tavily key and no ScrapingBee: the
    # direct fetch still carries the row.
    respx.post(f"{TAVILY_BASE_URL}/extract").mock(return_value=httpx.Response(402))
    respx.get("https://example.com/only-direct").mock(
        return_value=httpx.Response(
            200,
            text="<article><p>" + ("Solid article sentence. " * 5) + "</p></article>",
        )
    )
    async with ArticleFetcher(tavily_api_key="tav") as fetcher:
        result = await fetcher.fetch("https://example.com/only-direct")
    assert result.source == "direct"
    assert "Solid article sentence." in result.content


# ── Invalid URL ─────────────────────────────────────────────────────────────


@respx.mock
async def test_invalid_url_rejected_without_network_call() -> None:
    async with ArticleFetcher(tavily_api_key="tav") as fetcher:
        with pytest.raises(ArticleFetchError):
            await fetcher.fetch("not-a-url")
        with pytest.raises(ArticleFetchError):
            await fetcher.fetch("")
        with pytest.raises(ArticleFetchError):
            await fetcher.fetch("ftp://example.com/x")
    # No respx route ever invoked.
    assert not respx.routes


# ── max_chars truncation ────────────────────────────────────────────────────


@respx.mock
async def test_max_chars_truncates_long_articles() -> None:
    long_content = " ".join(["word"] * 50_000)  # ~250k chars
    respx.post(f"{TAVILY_BASE_URL}/extract").mock(
        return_value=httpx.Response(
            200, json={"results": [{"raw_content": long_content}]}
        )
    )
    async with ArticleFetcher(tavily_api_key="tav", max_chars=200) as fetcher:
        result = await fetcher.fetch("https://example.com/long")
    assert len(result.content) <= 200
    assert result.char_count == len(result.content)


# ── Cost constants positive ────────────────────────────────────────────────


def test_cost_constants_positive() -> None:
    assert COST_TAVILY_EXTRACT_USD > 0
    assert COST_SCRAPINGBEE_REQUEST_USD > 0
