"""Per-row content-safety detection.

Decides, before any LLM call, whether a row's Vertical column matches the
admin-tunable "sensitive apparel" keyword list. When it matches, downstream
prompt builders append a safety block that forbids depicting humans, body
parts, mannequins, etc., and constrains the voiceover to product attributes.

Detection is exact, case-insensitive substring matching against the row's
``vertical`` text — no LLM call, no per-row cost. The keyword list lives in
the SettingsStore under ``SETTING_SENSITIVE_APPAREL_KEYWORDS`` (see
``runtime_settings.py``) so it can be tuned without a redeploy.

Plan: ``_plans/2026-06-04-sensitive-apparel-safeguard-and-per-tab-prompts.md`` §3.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from bulkvid.logging import get_logger

_log = get_logger("safety")


# Re-import inside functions to avoid a circular import at module load
# (runtime_settings/settings_store don't import safety, but this keeps the
# graph one-way).


# ── Data ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SafetyContext:
    """Result of the per-row safety check.

    ``matched`` is the only field the prompt builders inspect; ``matched_keyword``
    is carried for logging so we can see *why* the safeguard fired.
    """

    matched: bool = False
    matched_keyword: str | None = None


SAFE = SafetyContext(matched=False, matched_keyword=None)


# ── Parsing the admin-edited keyword list ────────────────────────────────────


def parse_keywords(blob: str) -> tuple[str, ...]:
    """Turn the admin's free-form string into a clean, lowercase tuple.

    Accepts comma, newline, or semicolon separated input; trims whitespace;
    drops empty entries; lowercases everything. Defensive against the admin
    pasting a list with trailing commas, blank lines, or mixed case.
    """
    if not blob:
        return ()
    # Normalize separators to commas first so a single split handles all forms.
    normalized = blob.replace("\n", ",").replace(";", ",")
    items = [part.strip().lower() for part in normalized.split(",")]
    return tuple(item for item in items if item)


# ── Detection ────────────────────────────────────────────────────────────────


def detect_sensitive_apparel(
    vertical: str, keywords: Iterable[str]
) -> SafetyContext:
    """Return a ``SafetyContext`` describing whether ``vertical`` is sensitive.

    Match rule: lowercase substring. ``"Lingerie Boutique"`` matches because
    ``"lingerie"`` is in the keyword list. ``"Smart home gadgets"`` doesn't.
    An empty vertical never matches.
    """
    text = (vertical or "").strip().lower()
    if not text:
        return SAFE
    for kw in keywords:
        kw_norm = (kw or "").strip().lower()
        if kw_norm and kw_norm in text:
            return SafetyContext(matched=True, matched_keyword=kw_norm)
    return SAFE


# ── Prompt assembly ──────────────────────────────────────────────────────────


def append_safety_block(prompt: str, safety: SafetyContext, block: str) -> str:
    """Tack the safety block onto ``prompt`` when ``safety.matched``.

    A short delimiter line separates the original prompt from the safety
    block so the model sees a clear boundary. When the safeguard isn't
    triggered the prompt is returned unchanged.
    """
    if not safety.matched or not block.strip():
        return prompt
    separator = "\n\n—————\n"
    return f"{prompt}{separator}{block.strip()}"


# ── Convenience logger ───────────────────────────────────────────────────────


def log_detection(row_num: int, vertical: str, safety: SafetyContext) -> None:
    """One namespaced line per row so we can grep '[safety detect]' in logs."""
    _log.info(
        "safety_detect",
        row=row_num,
        matched=safety.matched,
        matched_keyword=safety.matched_keyword,
        vertical=(vertical or "")[:80],
    )


async def resolve_safety(
    settings_store: Any, vertical: str, row_num: int = 0
) -> SafetyContext:
    """One-call helper for row processors: load keywords from settings, detect, log.

    Pass the row's ``vertical`` field and (optionally) ``row_num``. Returns a
    ``SafetyContext`` ready to thread into every prompt builder. When the
    settings store is unavailable (e.g. unit tests that don't wire one up)
    falls back to a safe-empty context — better to ship without the safeguard
    than to crash a row over a missing dependency.
    """
    if settings_store is None:
        log_detection(row_num, vertical, SAFE)
        return SAFE
    # Imported lazily so safety.py can stay below runtime_settings in the
    # import graph if anyone wants to flatten it later.
    from bulkvid.orchestrator.runtime_settings import (
        SENSITIVE_APPAREL_KEYWORDS_DEFAULT,
        SETTING_SENSITIVE_APPAREL_KEYWORDS,
    )

    blob = await settings_store.get(
        SETTING_SENSITIVE_APPAREL_KEYWORDS,
        default=SENSITIVE_APPAREL_KEYWORDS_DEFAULT,
    )
    keywords = parse_keywords(blob)
    safety = detect_sensitive_apparel(vertical, keywords)
    log_detection(row_num, vertical, safety)
    return safety
