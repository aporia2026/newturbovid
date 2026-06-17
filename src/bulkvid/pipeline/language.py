"""Language detection — drives voiceover language from the article body.

The detected language drives the VO language (plan constraints, "the language
of the article which will determine the language of the voiceover"). Country
is secondary context for the script gen, not authoritative for TTS.

Uses gpt-5.4-mini for accuracy on short snippets across many languages.
An in-process LRU cache (hashed by article snippet) skips the LLM call when
the same article appears in multiple rows — common in bulk batches.

Cache parity with ``refs/creative_builder_dev`` ``KIE_CB_LANGUAGE_CACHE``:
  - keyed by ``sha256(article[:500])``
  - bounded at 256 entries (~12 KB)
  - per-process, restart-cheap, no GCS dependency

Plan §5 (Models), §9 (feature flag ``BULKVID_LANGUAGE_CACHE``), §15 Appendix B.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass, replace
from urllib.parse import parse_qs, urlparse

from bulkvid.adapters.openai_client import OpenAIClient
from bulkvid.logging import get_logger

_log = get_logger("language")


# ── Constants ────────────────────────────────────────────────────────────────


SUPPORTED_LANGUAGES = (
    "en", "he", "ar", "fr", "es", "de", "it", "pt", "nl", "pl",
    "ru", "tr", "ja", "ko", "zh", "vi", "th", "id", "hi", "sv",
    "no", "da", "fi", "cs", "el", "ro", "hu", "uk",
)
DEFAULT_LANGUAGE = "en"
DETECTION_MODEL = "gpt-5.4-mini"
CACHE_SIZE = 256
SNIPPET_LEN = 500


# ── Explicit-market signals (safety net for a wrong-language scrape) ─────────
#
# Article-language detection is primary, but a bad/transient scrape can hand us
# the wrong-language content — e.g. a programmatic page that served English at
# fetch time even though it's Spanish now. When detection conflicts with the
# operator's EXPLICIT market signal (the sheet Country column, or a
# ``locale=xx_YY`` in the pasted article URL), we prefer the explicit signal
# rather than ship a wrong-language video to a localized market. Chat
# 2026-06-17: an es_MX cartoon row went out in English for exactly this reason.

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

# URL query params that carry an explicit content locale, e.g. ``locale=es_MX``.
_LOCALE_PARAM_KEYS = frozenset({"locale"})


SYSTEM_PROMPT = (
    "You detect the primary language of an article body. "
    "Return ONLY a strict JSON object: "
    '{"language": "<ISO 639-1 lowercase>", "confidence": <0.0-1.0>}\n\n'
    "Rules:\n"
    "- Two-letter ISO 639-1 codes only (en, he, ar, fr, es, de, it, ...).\n"
    "- For multilingual text, return the dominant language.\n"
    f"- If you cannot tell, return {{\"language\": \"{DEFAULT_LANGUAGE}\", \"confidence\": 0.0}}.\n"
    "- Output NOTHING outside the JSON object."
)


# ── Result ───────────────────────────────────────────────────────────────────


@dataclass
class LanguageResult:
    language: str             # ISO 639-1 lowercase
    confidence: float         # 0.0 – 1.0
    cost_usd: float
    cached: bool


# ── Tiny LRU (event loop is single-threaded; no lock needed) ─────────────────


class _LRU:
    def __init__(self, maxsize: int = CACHE_SIZE) -> None:
        self._maxsize = maxsize
        self._data: OrderedDict[str, str] = OrderedDict()

    def get(self, key: str) -> str | None:
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        return None

    def put(self, key: str, value: str) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        self._data.clear()


_cache = _LRU()


def _cache_key(text: str) -> str:
    return hashlib.sha256(text[:SNIPPET_LEN].encode("utf-8", errors="ignore")).hexdigest()


# ── Public API ───────────────────────────────────────────────────────────────


async def detect_language(
    client: OpenAIClient,
    article_body: str,
    *,
    use_cache: bool = True,
    model: str = DETECTION_MODEL,
) -> LanguageResult:
    """Detect the primary language of an article body.

    Empty input short-circuits to the default language with zero cost.
    Malformed model output falls back to the default language but charges
    the call's cost (it ran).
    """
    text = (article_body or "").strip()
    if not text:
        return LanguageResult(
            language=DEFAULT_LANGUAGE, confidence=0.0, cost_usd=0.0, cached=False
        )

    key = _cache_key(text)
    if use_cache:
        cached_lang = _cache.get(key)
        if cached_lang is not None:
            _log.info("cache_hit", lang=cached_lang, cache_size=len(_cache))
            return LanguageResult(
                language=cached_lang, confidence=1.0, cost_usd=0.0, cached=True
            )

    snippet = text[:SNIPPET_LEN]
    _log.info("detect_submit", chars=len(snippet))

    result = await client.chat(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": snippet},
        ],
        response_format={"type": "json_object"},
        max_tokens=50,
        temperature=0.0,
    )

    try:
        parsed = json.loads(result.text)
    except json.JSONDecodeError as e:
        _log.error("detect_parse_failed", error=str(e), raw_preview=result.text[:200])
        return LanguageResult(
            language=DEFAULT_LANGUAGE,
            confidence=0.0,
            cost_usd=result.cost_usd,
            cached=False,
        )

    lang_raw = str(parsed.get("language") or "").strip().lower()
    lang = lang_raw[:2]
    if lang not in SUPPORTED_LANGUAGES:
        _log.warning("detect_unsupported_lang", returned=lang_raw)
        lang = DEFAULT_LANGUAGE

    confidence_raw = parsed.get("confidence")
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else 0.5
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.5

    if use_cache:
        _cache.put(key, lang)

    _log.info(
        "detect_ok",
        lang=lang,
        confidence=confidence,
        cost_usd=result.cost_usd,
        cache_size=len(_cache),
    )
    return LanguageResult(
        language=lang, confidence=confidence, cost_usd=result.cost_usd, cached=False
    )


# ── Reconciliation (explicit-market safety net) ──────────────────────────────


def parse_locale_language(url: str) -> str | None:
    """Language from a ``locale=xx_YY`` (or ``xx-YY`` / ``xx``) query param in
    ``url``, validated against ``SUPPORTED_LANGUAGES``. ``None`` when the param
    is absent or the language isn't supported."""
    if not url:
        return None
    try:
        qs = parse_qs(urlparse(url).query)
    except ValueError:
        return None
    for raw_key, values in qs.items():
        if raw_key.lower() in _LOCALE_PARAM_KEYS and values:
            head = values[0].strip().replace("-", "_").split("_", 1)[0][:2].lower()
            if head in SUPPORTED_LANGUAGES:
                return head
    return None


def expected_language(article_url: str, country: str) -> tuple[str | None, str | None]:
    """The language a row is *expected* to be in, from explicit operator
    signals: the Country column first (the deliberate market selection), then a
    ``locale=`` in the article URL. Returns ``(lang, signal)`` — signal is
    ``"country"`` or ``"locale"`` — or ``(None, None)`` when neither yields an
    unambiguous supported language (so detection stays authoritative)."""
    mapped = COUNTRY_TO_LANGUAGE.get((country or "").strip().upper())
    if mapped:
        return mapped, "country"
    loc = parse_locale_language(article_url)
    if loc:
        return loc, "locale"
    return None, None


def reconcile_language(
    detected: LanguageResult, *, article_url: str, country: str
) -> LanguageResult:
    """Safety net over ``detect_language`` — article detection stays primary.

    When the detected language CONFLICTS with the operator's explicit market
    signal (Country column, or a ``locale=`` in the article URL), prefer the
    explicit signal: a conflict almost always means the scrape returned
    wrong-language content (a bad/transient render), and shipping a
    wrong-language video to a localized market is the worst outcome. Logs
    ``language_conflict`` so misfires are visible in prod. With no explicit
    signal, or when it agrees with detection, the detected language is returned
    unchanged. Chat 2026-06-17 (es_MX cartoon row shipped in English because
    the source served English bytes at fetch time)."""
    expected, signal = expected_language(article_url, country)
    if expected is None or expected == detected.language:
        return detected
    _log.warning(
        "language_conflict",
        detected=detected.language,
        detected_confidence=round(detected.confidence, 3),
        expected=expected,
        signal=signal,
        url=(article_url or "")[:200],
    )
    return replace(detected, language=expected)


def clear_cache() -> None:
    """Test helper. Clears the in-process LRU."""
    _cache.clear()


def cache_size() -> int:
    """Inspection helper for the admin panel / tests."""
    return len(_cache)
