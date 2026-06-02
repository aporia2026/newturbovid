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
from dataclasses import dataclass

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


def clear_cache() -> None:
    """Test helper. Clears the in-process LRU."""
    _cache.clear()


def cache_size() -> int:
    """Inspection helper for the admin panel / tests."""
    return len(_cache)
