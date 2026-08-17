"""Explicit market signals — the country half of ``locale=xx_YY``.

Regression cover for chat 2026-08-17: rows pasted as
``www.drexur.com/dsr?q=...&locale=en_IE`` with a blank Country column shipped
with no accent and no market context, because ``locale=`` was read for its
language half only and the region was thrown away.

Plan: ``_plans/2026-08-17-locale-market-accent.md``.
"""

from __future__ import annotations

import pytest

from bulkvid.adapters.gemini_tts import accent_directive
from bulkvid.models.row import SimpleRow
from bulkvid.pipeline.market import (
    effective_country,
    expected_language,
    parse_locale_language,
    parse_locale_region,
)

# The URLs from chat, verbatim — including the schemeless form as pasted.
_EXAMPLES = [
    ("www.drexur.com/dsr?q=pet%20insurance%20for%20older%20dogs&locale=en_US", "en", "US"),
    ("www.drexur.com/dsr?q=seized%20cars%20for%20sale&locale=en_US", "en", "US"),
    ("www.drexur.com/dsr?q=seized%20cars%20ireland&locale=en_IE", "en", "IE"),
    ("www.drexur.com/dsr?q=seized%20cars%20canada&locale=en_CA", "en", "CA"),
    ("www.drexur.com/dsr?q=seized%20cars%20australia&locale=en_AU", "en", "AU"),
    ("www.drexur.com/dsr?q=takavarikoidut%20autot&locale=fi_FI", "fi", "FI"),
    ("www.drexur.com/dsr?q=utm%C3%A4tta%20bilar&locale=sv_SE", "sv", "SE"),
    ("www.drexur.com/dsr?q=seized%20cars%20auction&locale=en_AU", "en", "AU"),
]


# ── parse_locale_region ──────────────────────────────────────────────────────


@pytest.mark.parametrize("url,language,region", _EXAMPLES)
def test_example_urls_yield_language_and_region(url: str, language: str, region: str) -> None:
    """Every URL from chat resolves to both halves of its market."""
    assert parse_locale_language(url) == language
    assert parse_locale_region(url) == region


def test_region_hyphen_separator() -> None:
    assert parse_locale_region("https://x.com/a?locale=pt-BR") == "BR"


def test_region_is_uppercased() -> None:
    assert parse_locale_region("https://x.com/a?locale=en_ie") == "IE"


def test_region_key_case_insensitive() -> None:
    assert parse_locale_region("https://x.com/a?Locale=en_IE") == "IE"


def test_region_absent_for_bare_language() -> None:
    assert parse_locale_region("https://x.com/a?locale=fr") is None


def test_region_rejects_m49_grouping() -> None:
    # es_419 is a UN M49 region (Latin America), not a country — no accent
    # exists for it, so it must not reach the prompt.
    assert parse_locale_region("https://x.com/a?locale=es_419") is None


def test_region_rejects_non_alpha2() -> None:
    assert parse_locale_region("https://x.com/a?locale=en_USAA") is None


def test_region_absent_param() -> None:
    assert parse_locale_region("https://x.com/article") is None


def test_region_empty_url() -> None:
    assert parse_locale_region("") is None


def test_region_from_gl_param() -> None:
    assert parse_locale_region("https://x.com/a?gl=DE") == "DE"


def test_region_from_country_param() -> None:
    assert parse_locale_region("https://x.com/a?country=fr") == "FR"


def test_locale_region_beats_region_only_param() -> None:
    """A full locale is the more specific signal, so it wins."""
    assert parse_locale_region("https://x.com/a?locale=en_IE&gl=US") == "IE"


def test_region_ignores_unrelated_params() -> None:
    assert parse_locale_region("https://x.com/a?q=seized%20cars&utm_source=fb") is None


def test_garbage_url_does_not_raise() -> None:
    assert parse_locale_region("not a url at all ???") is None


# ── effective_country (Country column wins) ──────────────────────────────────


def test_effective_country_prefers_explicit_column() -> None:
    """The operator's deliberate selection is never overridden by the URL."""
    assert effective_country("https://x.com/a?locale=en_IE", "US") == "US"


def test_effective_country_fills_blank_from_locale() -> None:
    assert effective_country("https://x.com/a?locale=en_IE", "") == "IE"


def test_effective_country_treats_whitespace_column_as_blank() -> None:
    assert effective_country("https://x.com/a?locale=en_AU", "   ") == "AU"


def test_effective_country_strips_explicit_column() -> None:
    assert effective_country("https://x.com/a", "  US  ") == "US"


def test_effective_country_empty_when_no_signal() -> None:
    assert effective_country("https://x.com/article", "") == ""


# ── Row construction (the choke point) ───────────────────────────────────────


def _row(article_url: str, country: str) -> SimpleRow:
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


def test_row_fills_blank_country_from_url() -> None:
    assert _row(_EXAMPLES[2][0], "").country == "IE"


def test_row_keeps_explicit_country() -> None:
    assert _row(_EXAMPLES[2][0], "US").country == "US"


def test_row_without_market_signal_stays_blank() -> None:
    assert _row("https://x.com/article", "").country == ""


def test_row_positional_construction_still_works() -> None:
    """The mixin declares no fields, so field order is unchanged."""
    row = SimpleRow(2, "", "auto", _EXAMPLES[4][0], "", True, False, "9:16", "How To", "")
    assert row.country == "AU"
    assert row.row_num == 2
    assert row.vertical == "auto"


# ── End to end: the accent the row actually gets ─────────────────────────────


def test_blank_country_row_now_gets_its_accent() -> None:
    """The bug, end to end: an Irish row with a blank Country column."""
    row = _row(_EXAMPLES[2][0], "")
    assert accent_directive("en", row.country) == "Speak in a natural Irish English accent."


@pytest.mark.parametrize(
    "index,accent",
    [(0, "American"), (2, "Irish"), (3, "Canadian"), (4, "Australian")],
)
def test_english_examples_get_distinct_accents(index: int, accent: str) -> None:
    """en_US / en_IE / en_CA / en_AU are four markets, not one."""
    row = _row(_EXAMPLES[index][0], "")
    assert accent_directive("en", row.country) == f"Speak in a natural {accent} English accent."


def test_non_english_example_gets_regional_directive() -> None:
    row = _row(_EXAMPLES[5][0], "")          # fi_FI
    assert accent_directive("fi", row.country) == (
        "Use the natural regional accent and dialect spoken in Finland."
    )


# ── Deriving the country must not flip the row's language ────────────────────


def test_english_campaign_in_a_non_english_market_keeps_its_language() -> None:
    """``en_FI`` end to end: filling the country from the region must not make
    ``expected_language`` infer Finnish from FI and override the locale's own
    English. Guards the regression the 2026-08-17 derivation would otherwise
    have introduced."""
    row = _row("https://x.com/a?locale=en_FI", "")
    assert row.country == "FI"
    assert expected_language(row.article_url, row.country) == ("en", "locale")


def test_english_campaign_in_a_non_english_market_gets_no_bogus_accent() -> None:
    """No English accent exists for FI, so the directive is empty rather than a
    made-up "Finnish-accented English"."""
    row = _row("https://x.com/a?locale=en_FI", "")
    assert accent_directive("en", row.country) == ""
