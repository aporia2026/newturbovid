"""Per-language default CTA text for the simple_x4 card overlay.

Yoav 2026-06-08: when the operator leaves a row's CTA cell blank, the card
should show "Learn More" in the article's language — phrased the way a
native speaker would naturally say it on a CTA pill, NOT a literal
word-for-word translation.

Curated table below. Strings are written/picked by hand to match the
conversational marketing register; auto-translation would frequently
produce awkward ones ("Aprende Más" reads like a school motto in Spanish,
where "Saber Más" or "Más Información" is what ads actually use).

Unknown language → fall back to English ("Learn More") rather than guessing
or leaving the pill empty. Add a new entry to the table when a new language
shows up in the pipeline.

Plan: ``_plans/2026-06-08-simple-x4-template-cards.md`` §D.5 (CTA fallback).
"""

from __future__ import annotations

from typing import Final

# Keyed by ISO-639-1 language code (lowercase) as returned by
# ``bulkvid.pipeline.language.detect_language``. The right-arrow suffix
# matches the visual style of the original mockup CTAs ("DISCOVER MORE >>")
# so a Learn-More fallback feels consistent with operator-typed CTAs.
_LEARN_MORE_BY_LANGUAGE: Final[dict[str, str]] = {
    "en": "Learn More >>",
    "es": "Saber Más >>",
    "pt": "Saiba Mais >>",
    "fr": "En Savoir Plus >>",
    "de": "Mehr Erfahren >>",
    "it": "Scopri di Più >>",
    "nl": "Meer Weten >>",
    "pl": "Dowiedz Się Więcej >>",
    "ru": "Узнать Больше >>",
    "uk": "Дізнатися Більше >>",
    "ro": "Află Mai Mult >>",
    "el": "Μάθετε Περισσότερα >>",
    "tr": "Daha Fazla Bilgi >>",
    "he": "למידע נוסף >>",
    "ar": "اعرف المزيد >>",
    "fa": "بیشتر بدانید >>",
    "hi": "और जानें >>",
    "id": "Pelajari Lebih Lanjut >>",
    "th": "ดูเพิ่มเติม >>",
    "vi": "Tìm Hiểu Thêm >>",
    "ja": "詳しく見る >>",
    "ko": "자세히 보기 >>",
    "zh": "了解更多 >>",
    "sv": "Läs Mer >>",
    "no": "Les Mer >>",
    "da": "Læs Mere >>",
    "fi": "Lue Lisää >>",
    "cs": "Více Informací >>",
    "sk": "Viac Informácií >>",
    "hu": "Tudj Meg Többet >>",
}

DEFAULT_CTA_FALLBACK: Final[str] = "Learn More >>"


def default_cta_for_language(language: str) -> str:
    """Return the "Learn More" CTA phrased naturally for ``language``.

    ``language`` is normalised to a lowercase 2-letter code; longer values
    (e.g. ``"en-US"``) are truncated to their primary subtag. Unknown
    languages fall back to English ``"Learn More >>"`` rather than raising —
    a never-blank CTA is better than a failed render.
    """
    code = (language or "").strip().lower()[:2]
    return _LEARN_MORE_BY_LANGUAGE.get(code, DEFAULT_CTA_FALLBACK)
