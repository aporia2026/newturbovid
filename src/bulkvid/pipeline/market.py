"""Explicit market signals — the language and country a row is *meant* for.

The operator states a target market two ways: the sheet's Country column, and a
``locale=xx_YY`` in the pasted article URL. Both are deliberate choices, so both
outrank whatever the scraper happened to return.

  * LANGUAGE (``xx``) drives the voiceover language — see ``pipeline.language``,
    where detection stays primary and this is the safety net over it.
  * COUNTRY (``YY``) drives the accent and the script's market context — a row
    on ``locale=en_IE`` is an Irish row, not a generic English one.

Kept stdlib-only and dependency-free on purpose: ``models.row`` imports it to
fill a blank Country column at row construction, and must not drag in the
OpenAI SDK that ``pipeline.language`` pulls. ``pipeline.language`` re-exports
everything here, so callers may import from either.

Plan: ``_plans/2026-08-17-locale-market-accent.md``.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

# ── Languages ────────────────────────────────────────────────────────────────


SUPPORTED_LANGUAGES = (
    "en", "he", "ar", "fr", "es", "de", "it", "pt", "nl", "pl",
    "ru", "tr", "ja", "ko", "zh", "vi", "th", "id", "hi", "sv",
    "no", "da", "fi", "cs", "el", "ro", "hu", "uk",
)
DEFAULT_LANGUAGE = "en"


# ── Country → language (safety net for a wrong-language scrape) ──────────────
#
# Article-language detection is primary, but a bad/transient scrape can hand us
# the wrong-language content — e.g. a programmatic page that served English at
# fetch time even though it's Spanish now. When detection conflicts with the
# operator's EXPLICIT market signal, we prefer the explicit signal rather than
# ship a wrong-language video to a localized market. Chat 2026-06-17: an es_MX
# cartoon row went out in English for exactly this reason.
#
# Country (ISO 3166-1 alpha-2) → language (ISO 639-1), UNAMBIGUOUS markets
# only. Multilingual countries (CH, BE, CA, IN, ...) are deliberately omitted
# so they fall through to article detection / URL locale instead of being
# forced to one language. Every value is in ``SUPPORTED_LANGUAGES``.
COUNTRY_TO_LANGUAGE: dict[str, str] = {
    # Spanish-speaking markets
    "MX": "es", "ES": "es", "AR": "es", "CO": "es", "CL": "es", "PE": "es",
    "VE": "es", "EC": "es", "GT": "es", "CU": "es", "BO": "es", "DO": "es",
    "HN": "es", "PY": "es", "SV": "es", "NI": "es", "CR": "es", "PA": "es",
    "UY": "es",
    # English
    "US": "en", "GB": "en", "UK": "en", "AU": "en", "NZ": "en", "IE": "en",
    # Portuguese
    "BR": "pt", "PT": "pt",
    # Other single-dominant-language markets
    "FR": "fr", "DE": "de", "AT": "de", "IT": "it", "NL": "nl", "PL": "pl",
    "RU": "ru", "TR": "tr", "JP": "ja", "KR": "ko", "CN": "zh", "TW": "zh",
    "VN": "vi", "TH": "th", "ID": "id", "SE": "sv", "NO": "no", "DK": "da",
    "FI": "fi", "CZ": "cs", "GR": "el", "RO": "ro", "HU": "hu", "UA": "uk",
    "IL": "he",
    # Arabic-dominant markets
    "SA": "ar", "AE": "ar", "EG": "ar", "JO": "ar", "KW": "ar", "QA": "ar",
    "OM": "ar", "BH": "ar", "LB": "ar", "IQ": "ar", "LY": "ar", "MA": "ar",
    "DZ": "ar", "TN": "ar",
}


# ── URL query params carrying an explicit market ─────────────────────────────

# Full content locale, e.g. ``locale=es_MX`` — carries language AND region.
_LOCALE_PARAM_KEYS = frozenset({"locale"})

# Region-only params. ``gl`` is the long-standing Google "geolocation" key;
# ``country`` and ``region`` show up on hand-built campaign URLs. Read for the
# country only — they say nothing about the content language.
_REGION_PARAM_KEYS = frozenset({"gl", "country", "region"})


# ── Parsing ──────────────────────────────────────────────────────────────────


def _query_params(url: str) -> dict[str, list[str]]:
    """Query params of ``url``, lowercased keys. ``{}`` for anything unparseable.

    Schemeless URLs are supported — the sheet is routinely pasted as
    ``www.example.com/p?locale=en_IE``, and ``urlparse`` still finds the query.
    """
    if not url:
        return {}
    try:
        parsed = parse_qs(urlparse(url).query)
    except ValueError:
        return {}
    return {key.lower(): values for key, values in parsed.items()}


def _locale_values(params: dict[str, list[str]]) -> list[str]:
    """Every non-empty ``locale=`` value, normalized to ``xx_YY`` separators."""
    out: list[str] = []
    for key in _LOCALE_PARAM_KEYS:
        for raw in params.get(key, ()):
            value = (raw or "").strip().replace("-", "_")
            if value:
                out.append(value)
    return out


def parse_locale_language(url: str) -> str | None:
    """Language from a ``locale=xx_YY`` (or ``xx-YY`` / ``xx``) query param in
    ``url``, validated against ``SUPPORTED_LANGUAGES``. ``None`` when the param
    is absent or the language isn't supported."""
    for value in _locale_values(_query_params(url)):
        head = value.split("_", 1)[0][:2].lower()
        if head in SUPPORTED_LANGUAGES:
            return head
    return None


def parse_locale_region(url: str) -> str | None:
    """Country (ISO 3166-1 alpha-2, uppercase) from ``url``'s market params.

    Reads the region half of ``locale=xx_YY`` first, then the region-only params
    (``gl`` / ``country`` / ``region``). ``None`` when nothing carries a region:
    a bare ``locale=fr`` has none, and ``es_419`` is a UN M49 grouping rather
    than a country, so neither yields one.

    Only two ASCII letters are ever returned. That both rejects junk and keeps
    an operator-supplied value from reaching a prompt as free text.
    """
    params = _query_params(url)

    for value in _locale_values(params):
        parts = value.split("_", 1)
        if len(parts) == 2 and _is_country_code(parts[1]):
            return parts[1][:2].upper()

    for key in _REGION_PARAM_KEYS:
        for raw in params.get(key, ()):
            value = (raw or "").strip()
            if _is_country_code(value):
                return value[:2].upper()

    return None


def _is_country_code(value: str) -> bool:
    return len(value) == 2 and value.isascii() and value.isalpha()


# ── Resolution ───────────────────────────────────────────────────────────────


def effective_country(article_url: str, country: str) -> str:
    """The row's target country: the Country column, else the URL locale region.

    The Country column is the operator's deliberate selection and always wins;
    the URL region only ever fills a blank. Returns ``""`` when neither carries
    one, which every consumer already treats as "no market signal".
    """
    explicit = (country or "").strip()
    if explicit:
        return explicit
    return parse_locale_region(article_url) or ""


def expected_language(article_url: str, country: str) -> tuple[str | None, str | None]:
    """The language a row is *expected* to be in, from explicit operator
    signals. Returns ``(lang, signal)`` — signal is ``"country"`` or
    ``"locale"`` — or ``(None, None)`` when neither yields an unambiguous
    supported language (so detection stays authoritative).

    A full ``locale=xx_YY`` states the content language outright, so it beats
    inferring one from the country: ``en_FI`` is an English campaign aimed at
    Finland, and ``COUNTRY_TO_LANGUAGE["FI"]`` would wrongly call it Finnish.
    That matters now that a blank Country column is filled from the locale
    region (``effective_country``) — without this, deriving ``FI`` would flip
    the row's own stated language.

    The Country column still wins when it names a *different* market than the
    URL, which is the operator correcting a stale or copied-in URL — the
    deliberate per-campaign selection, not a guess.
    """
    explicit = (country or "").strip().upper()
    locale_language = parse_locale_language(article_url)

    if locale_language and explicit in ("", parse_locale_region(article_url) or ""):
        return locale_language, "locale"

    mapped = COUNTRY_TO_LANGUAGE.get(explicit)
    if mapped:
        return mapped, "country"
    if locale_language:
        return locale_language, "locale"
    return None, None
