"""Explicit-market language safety net (``reconcile_language``).

Article-language detection stays primary, but when it conflicts with the
operator's explicit market signal (Country column / URL ``locale=``) we prefer
the explicit signal — a conflict almost always means the scrape returned
wrong-language content. Regression cover for chat 2026-06-17: an ``es_MX``
cartoon row shipped in English because the source served English bytes at
fetch time.
"""

from __future__ import annotations

from bulkvid.pipeline.language import (
    LanguageResult,
    expected_language,
    parse_locale_language,
    reconcile_language,
)

_MX_URL = "https://www.drexur.com/dsr?q=cursos%20contable&locale=es_MX"


def _det(lang: str, confidence: float = 0.99) -> LanguageResult:
    return LanguageResult(language=lang, confidence=confidence, cost_usd=0.0002, cached=False)


# ── parse_locale_language ────────────────────────────────────────────────────


def test_parse_locale_underscore() -> None:
    assert parse_locale_language("https://x.com/a?locale=es_MX") == "es"


def test_parse_locale_hyphen() -> None:
    assert parse_locale_language("https://x.com/a?locale=pt-BR") == "pt"


def test_parse_locale_bare_lang() -> None:
    assert parse_locale_language("https://x.com/a?locale=fr") == "fr"


def test_parse_locale_region_only_code() -> None:
    # es_419 (Latin America) → es
    assert parse_locale_language("https://x.com/a?locale=es_419") == "es"


def test_parse_locale_case_insensitive_key() -> None:
    assert parse_locale_language("https://x.com/a?Locale=es_MX") == "es"


def test_parse_locale_unsupported_returns_none() -> None:
    assert parse_locale_language("https://x.com/a?locale=xx_YY") is None


def test_parse_locale_absent_returns_none() -> None:
    assert parse_locale_language("https://x.com/article") is None


def test_parse_locale_empty_url() -> None:
    assert parse_locale_language("") is None


# ── expected_language (Country first, then URL locale) ───────────────────────


def test_expected_country_wins() -> None:
    assert expected_language(_MX_URL, "MX") == ("es", "country")


def test_expected_country_case_insensitive() -> None:
    assert expected_language("https://x.com/a", "mx") == ("es", "country")


def test_expected_locale_fallback_when_country_blank() -> None:
    assert expected_language(_MX_URL, "") == ("es", "locale")


def test_expected_multilingual_country_falls_through_to_locale() -> None:
    # IN (India) is deliberately NOT in the map; the URL locale resolves it.
    assert expected_language("https://x.com/a?locale=hi_IN", "IN") == ("hi", "locale")


def test_expected_none_when_no_signal() -> None:
    assert expected_language("https://x.com/article", "") == (None, None)


# ── reconcile_language ───────────────────────────────────────────────────────


def test_reconcile_conflict_prefers_country() -> None:
    """The r8 regression: detection said English, the row is MX → Spanish."""
    out = reconcile_language(_det("en"), article_url=_MX_URL, country="MX")
    assert out.language == "es"
    # Non-language fields are preserved through the override.
    assert out.confidence == 0.99
    assert out.cost_usd == 0.0002
    assert out.cached is False


def test_reconcile_conflict_prefers_locale_when_country_blank() -> None:
    out = reconcile_language(_det("en"), article_url=_MX_URL, country="")
    assert out.language == "es"


def test_reconcile_agreement_is_passthrough() -> None:
    det = _det("es")
    out = reconcile_language(det, article_url=_MX_URL, country="MX")
    assert out is det          # unchanged object, detection honoured


def test_reconcile_no_signal_is_passthrough() -> None:
    det = _det("en")
    out = reconcile_language(det, article_url="https://x.com/article", country="")
    assert out is det          # detection stays authoritative when no market signal


def test_reconcile_does_not_override_matching_unusual_language() -> None:
    # A genuinely French article on a FR row stays French (no spurious override).
    det = _det("fr")
    out = reconcile_language(det, article_url="https://x.com/a", country="FR")
    assert out is det
