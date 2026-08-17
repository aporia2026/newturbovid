"""Scheme-less pasted URLs must fetch, not fail.

Regression cover for chat 2026-08-17: an entire batch failed with
``Invalid URL: 'www.drexur.com/dsr?q=...'`` and produced no video for any row.
``ArticleFetcher.fetch`` required an ``http(s)://`` prefix, but the sheet cells
were pasted without one — a browser adds the scheme silently, so the cell looks
correct to the operator.

Plan: ``_plans/2026-08-17-schemeless-article-url.md``.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from bulkvid.adapters.article_fetch import (
    SCRAPINGBEE_BASE_URL,
    ArticleFetcher,
    ArticleFetchError,
)
from bulkvid.models.row import SimpleRow
from bulkvid.pipeline.urls import normalize_url

# The failing rows from the sidebar screenshot, exactly as pasted.
_FAILED_ROWS = [
    "www.drexur.com/dsr?q=pet%20insurance%20for%20older%20dogs&locale=en_US",
    "www.drexur.com/dsr?q=seized%20cars%20for%20sale&locale=en_US",
    "www.drexur.com/dsr?q=seized%20cars%20ireland&locale=en_IE",
    "www.drexur.com/dsr?q=seized%20cars%20canada&locale=en_CA",
    "www.drexur.com/dsr?q=seized%20cars%20australia&locale=en_AU",
    "www.drexur.com/dsr?q=takavarikoidut%20autot&locale=fi_FI",
    "www.drexur.com/dsr?q=utm%C3%A4tta%20bilar&locale=sv_SE",
]


# ── normalize_url ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", _FAILED_ROWS)
def test_every_failed_row_url_becomes_fetchable(url: str) -> None:
    assert normalize_url(url) == "https://" + url


def test_query_string_is_preserved_exactly() -> None:
    """The locale and q= params must survive untouched — they drive the market."""
    out = normalize_url("www.drexur.com/dsr?q=seized%20cars%20ireland&locale=en_IE")
    assert out.endswith("?q=seized%20cars%20ireland&locale=en_IE")


def test_https_is_left_alone() -> None:
    assert normalize_url("https://x.com/a") == "https://x.com/a"


def test_http_is_left_alone() -> None:
    assert normalize_url("http://x.com/a") == "http://x.com/a"


def test_surrounding_whitespace_is_trimmed() -> None:
    assert normalize_url("  www.x.com/a  ") == "https://www.x.com/a"


def test_protocol_relative_gets_https() -> None:
    assert normalize_url("//x.com/a") == "https://x.com/a"


def test_bare_domain_without_path() -> None:
    assert normalize_url("drexur.com") == "https://drexur.com"


def test_other_scheme_left_alone() -> None:
    assert normalize_url("ftp://x.com/a") == "ftp://x.com/a"


def test_empty_stays_empty() -> None:
    assert normalize_url("") == ""


@pytest.mark.parametrize("junk", ["seized cars ireland", "n/a", "TBD", "see comments"])
def test_free_text_is_not_turned_into_a_url(junk: str) -> None:
    """Junk in the cell must still fail loudly rather than become a bogus request."""
    assert normalize_url(junk) == junk


def test_none_is_safe() -> None:
    assert normalize_url(None) == ""      # type: ignore[arg-type]


# ── The guard that actually rejected them ────────────────────────────────────


@respx.mock
@pytest.mark.parametrize("url", _FAILED_ROWS)
async def test_fetch_no_longer_rejects_scheme_less_urls(url: str) -> None:
    """Before the fix every one of these raised ``Invalid URL`` before any
    network call. Now they reach ScrapingBee with the scheme filled in."""
    route = respx.get(SCRAPINGBEE_BASE_URL).mock(
        return_value=httpx.Response(200, text="<p>" + "Real article body. " * 20 + "</p>")
    )
    async with ArticleFetcher(scrapingbee_api_key="sb") as fetcher:
        result = await fetcher.fetch(url)

    assert route.called
    assert dict(respx.calls.last.request.url.params)["url"] == "https://" + url
    assert "Real article body." in result.content


@respx.mock
async def test_fetch_still_rejects_genuine_junk() -> None:
    async with ArticleFetcher(scrapingbee_api_key="sb") as fetcher:
        with pytest.raises(ArticleFetchError, match="Invalid URL"):
            await fetcher.fetch("not a url at all")
    assert not respx.routes


@respx.mock
async def test_fetch_still_rejects_empty() -> None:
    async with ArticleFetcher(scrapingbee_api_key="sb") as fetcher:
        with pytest.raises(ArticleFetchError, match="Invalid URL"):
            await fetcher.fetch("")
    assert not respx.routes


# ── Row construction normalizes too ──────────────────────────────────────────


def _row(article_url: str, country: str = "") -> SimpleRow:
    return SimpleRow(
        row_num=2,
        country=country,
        vertical="auto",
        article_url=article_url,
        manual_image_url="",
        voice_over=True,
        zapcap=False,
        aspect_ratio="9:16",
        script_pattern="How To",
        open_comments="",
    )


def test_row_normalizes_its_article_url() -> None:
    assert _row(_FAILED_ROWS[0]).article_url == "https://" + _FAILED_ROWS[0]


def test_row_still_derives_country_from_a_scheme_less_url() -> None:
    """Normalization must not disturb the locale parsing added earlier."""
    assert _row(_FAILED_ROWS[2]).country == "IE"


def test_row_keeps_explicit_country_and_still_fixes_the_url() -> None:
    row = _row(_FAILED_ROWS[2], "US")
    assert row.country == "US"
    assert row.article_url.startswith("https://")
