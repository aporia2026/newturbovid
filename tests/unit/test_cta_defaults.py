"""Tests for the per-language CTA default table.

The "Learn More" fallback is what gets drawn on the card pill when the
operator leaves both the row's CTA cell AND the admin-side override blank.
Translation needs to feel natural — not robotic — and the function must
never return empty so the renderer always has something to draw.

Plan: ``_plans/2026-06-08-simple-x4-template-cards.md`` §D.5.
"""

from __future__ import annotations

import pytest

from bulkvid.pipeline.cta_defaults import (
    DEFAULT_CTA_FALLBACK,
    default_cta_for_language,
)


# ── Languages we definitely support (matches what detect_language returns) ──


@pytest.mark.parametrize(
    "language,expected_substring",
    [
        ("en", "Learn More"),
        ("es", "Saber Más"),
        ("pt", "Saiba Mais"),
        ("fr", "En Savoir Plus"),
        ("de", "Mehr Erfahren"),
        ("it", "Scopri di Più"),
        ("he", "למידע נוסף"),
        ("ar", "اعرف المزيد"),
        ("ru", "Узнать Больше"),
        ("ja", "詳しく見る"),
        ("zh", "了解更多"),
    ],
)
def test_known_languages_return_natural_translation(
    language: str, expected_substring: str
) -> None:
    cta = default_cta_for_language(language)
    assert expected_substring in cta, (
        f"expected {expected_substring!r} in CTA for {language!r}, got {cta!r}"
    )


# ── Normalisation ───────────────────────────────────────────────────────────


def test_uppercase_language_code_is_handled() -> None:
    """detect_language might return 'EN' or 'En' — table is keyed by lowercase."""
    assert default_cta_for_language("ES") == default_cta_for_language("es")
    assert default_cta_for_language("De") == default_cta_for_language("de")


def test_locale_suffix_is_truncated() -> None:
    """``en-US`` should resolve to the same value as ``en``."""
    assert default_cta_for_language("en-US") == default_cta_for_language("en")
    assert default_cta_for_language("pt-BR") == default_cta_for_language("pt")
    assert default_cta_for_language("zh-CN") == default_cta_for_language("zh")


# ── Unknown / edge cases ────────────────────────────────────────────────────


def test_unknown_language_falls_back_to_english() -> None:
    """Never raise on a language we don't know about — fall back gracefully."""
    assert default_cta_for_language("xx") == DEFAULT_CTA_FALLBACK
    assert default_cta_for_language("klingon") == DEFAULT_CTA_FALLBACK


def test_empty_language_falls_back_to_english() -> None:
    """A row whose language detection failed shouldn't break the render."""
    assert default_cta_for_language("") == DEFAULT_CTA_FALLBACK
    assert default_cta_for_language(None) == DEFAULT_CTA_FALLBACK    # type: ignore[arg-type]


def test_returned_string_is_never_empty() -> None:
    """Defense in depth — no input should ever produce an empty CTA."""
    for lang in ("", "en", "xx", "ES", "zh-CN", None):    # type: ignore[arg-type]
        cta = default_cta_for_language(lang)
        assert cta, f"CTA empty for language {lang!r}"
