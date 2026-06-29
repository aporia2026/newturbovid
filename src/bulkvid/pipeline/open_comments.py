"""Open Comments classifier — the highest-priority signal.

The Sheet's 'Open Comments' column overrides everything else when present
(plan constraints, plan §15 Appendix B). Users put anything there:

  - TONE bias        — short stylistic hints ("urgent", "casual", "luxury")
  - DIRECTIVE        — explicit content rules ("mention $9.99", "CTA = Learn More")
  - OVERRIDE         — a complete ~10s script they want used verbatim
  - MIXED            — combination of the above
  - NONE             — empty cell

This module classifies the raw text and extracts each kind of signal so the
script generator (next pipeline module) can apply them cleanly without
re-doing the LLM-level interpretation.

Model: gpt-5.4-mini (cheap, fast, deterministic at temperature 0).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

from bulkvid.adapters.openai_client import MODEL_CLASSIFIER, OpenAIClient
from bulkvid.logging import get_logger

_log = get_logger("opencomments")


class OpenCommentsMode(str, Enum):
    NONE = "none"
    TONE = "tone"
    DIRECTIVE = "directive"
    OVERRIDE = "override"
    MIXED = "mixed"


@dataclass
class OpenCommentsAnalysis:
    mode: OpenCommentsMode
    raw_text: str
    tone_hints: list[str] = field(default_factory=list)
    directives: list[str] = field(default_factory=list)
    override_script: str | None = None
    cost_usd: float = 0.0
    # True when an OVERRIDE script is longer than the soft cap. Never blocks the
    # row — it's a visible "this will make a long video" flag for the operator
    # (surfaced in row metadata) and a guardrail the cartoon auto-fit clamps to.
    override_oversize: bool = False


# Soft word cap for an operator-pinned script. We never truncate the operator's
# copy (that would mangle approved ad text mid-sentence); past this we flag the
# row so a 60-word paste doesn't silently ship a 30s+ "Short". ~60 words ≈ 30s
# of speech at the observed Gemini TTS rate. Single source of truth shared by
# the script tabs (script_gen) and the cartoon auto-fit.
# Plan _plans/2026-06-29-pinned-script-open-comments-all-tabs.md.
OVERRIDE_SOFT_MAX_WORDS = 60


# Prefix markers that pin the cell as a verbatim script (the manager's
# "use this script:" convention). Matched case-insensitively at the START of the
# cell only — a marker mid-cell never triggers — longest first so "use this
# script" wins over "use script". Every marker leads with the imperative "use"
# on purpose: a bare "script:" prefix is ambiguous (operators write
# "script should be upbeat" as a DIRECTIVE), so we require the explicit verb to
# avoid hijacking notes. Stripping the marker BEFORE it reaches TTS is what keeps
# it out of the spoken audio, and therefore out of ZapCap's transcription-based
# captions. Plan _plans/2026-06-29-pinned-script-open-comments-all-tabs.md.
_PINNED_SCRIPT_MARKERS: tuple[str, ...] = (
    "use the following script",
    "use this exact script",
    "use this script",
    "use the script",
    "use script",
)

# Separators an operator (or their phone's autocorrect) might type between the
# marker and the script: ascii + full-width colon, hyphen, en/em dash, equals.
_MARKER_SEPARATORS = ":：-–—="  # noqa: RUF001 — ambiguous chars are intentional separators


def detect_pinned_script(raw_text: str) -> str | None:
    """Return the verbatim script when the cell starts with a pin marker.

    Deterministic, no LLM call. Case-insensitive and prefix-only (a marker in
    the middle of the cell never fires). Tolerant of what a lazy operator
    actually types — extra spaces, ``USE THIS SCRIPT -``, a full-width
    autocorrect colon. Returns the script with the marker and one trailing
    separator stripped, or ``None`` when there's no marker or nothing usable is
    left after it (so ``script:`` alone falls through, not an empty override).
    """
    text = (raw_text or "").strip()
    if not text:
        return None
    lowered = text.lower()
    for marker in _PINNED_SCRIPT_MARKERS:
        if not lowered.startswith(marker):
            continue
        rest = text[len(marker):]
        # The marker must be a whole token: the next char is a separator or
        # whitespace, never a letter — so "scripture…" / "use scripts…" do not
        # match "script" / "use script". A marker that is the entire cell
        # (rest == "") has no script after it and is handled below.
        if rest and not (rest[0].isspace() or rest[0] in _MARKER_SEPARATORS):
            continue
        rest = rest.lstrip().lstrip(_MARKER_SEPARATORS).strip()
        return rest or None
    return None


SYSTEM_PROMPT = """You classify 'Open Comments' from a bulk video creative sheet.

The cell can contain any combination of:
  - TONE bias: short stylistic hints ("urgent", "casual", "luxury", "playful")
  - DIRECTIVE: explicit content rules ("mention price $9.99", "CTA: Learn More", "include the word 'free'")
  - OVERRIDE: a complete ready-to-read script of roughly 20-45 words (~10s of speech)
  - MIXED: any combination

Return strict JSON with these exact fields:
{
  "mode": "none" | "tone" | "directive" | "override" | "mixed",
  "tone_hints": ["urgent", ...],
  "directives": ["mention $9.99", ...],
  "override_script": "..."  // or null
}

Decision rules:
- Empty / whitespace input -> mode "none"
- 20+ words of natural narrative prose (not instructions) -> mode "override",
  put the entire usable script in override_script
- Short style tags (1-5 words like "urgent" or "warm tone") -> mode "tone"
- Imperative content rules ("must include X", "CTA: Y") -> mode "directive"
- Anything blending the above -> mode "mixed", populate every relevant field

Be precise. Tone hints are adjectives/short phrases. Directives are imperatives.
Override is finished prose that could be spoken as a 10-second voiceover."""


def _audit_override(analysis: OpenCommentsAnalysis, *, source: str) -> None:
    """Emit the verbatim-override audit line (Yoav 2026-06-29 compliance call).

    Records WHICH row pinned a verbatim script, a content hash, and the word
    count, so a human can audit what shipped without storing the raw ad copy in
    the clear. ``source`` is ``"marker"`` (explicit ``use this script:``) or
    ``"auto"`` (LLM-detected bare paste). Row/batch context rides on the logger
    via ``set_context``.
    """
    script = (analysis.override_script or "").strip()
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()[:16]
    _log.info(
        "open_comments_override",
        source=source,
        script_words=len(script.split()),
        script_chars=len(script),
        script_sha256=digest,
        oversize=analysis.override_oversize,
    )


async def classify_open_comments(
    client: OpenAIClient,
    raw_text: str,
    model: str = MODEL_CLASSIFIER,
) -> OpenCommentsAnalysis:
    """Classify Open Comments into a mode + extracted fields.

    Empty input short-circuits without an LLM call.
    Malformed JSON from the model degrades gracefully to TONE with the full
    raw text as a single tone hint — we never block a row on classifier failure.
    """
    text = (raw_text or "").strip()
    if not text:
        return OpenCommentsAnalysis(
            mode=OpenCommentsMode.NONE, raw_text=""
        )

    # Deterministic, zero-cost path: the operator pinned a verbatim script with
    # an explicit "use this script:" marker. Short-circuit BEFORE the LLM —
    # reliable, free, and immune to the classifier guessing wrong. Every tab
    # (script + cartoon) inherits OVERRIDE from this one seam.
    pinned = detect_pinned_script(text)
    if pinned is not None:
        analysis = OpenCommentsAnalysis(
            mode=OpenCommentsMode.OVERRIDE,
            raw_text=text,
            override_script=pinned,
            override_oversize=len(pinned.split()) > OVERRIDE_SOFT_MAX_WORDS,
        )
        _audit_override(analysis, source="marker")
        return analysis

    _log.info("classify_submit", chars=len(text))

    result = await client.chat(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        response_format={"type": "json_object"},
        max_tokens=500,
        temperature=0.0,
    )

    try:
        parsed = json.loads(result.text)
    except json.JSONDecodeError as e:
        _log.error(
            "classify_json_parse_failed",
            error=str(e),
            raw_preview=result.text[:200],
        )
        return OpenCommentsAnalysis(
            mode=OpenCommentsMode.TONE,
            raw_text=text,
            tone_hints=[text],
            cost_usd=result.cost_usd,
        )

    mode_str = (parsed.get("mode") or "none").lower()
    try:
        mode = OpenCommentsMode(mode_str)
    except ValueError:
        _log.warning("classify_unknown_mode", returned_mode=mode_str)
        mode = OpenCommentsMode.TONE

    tone_hints = [str(t).strip() for t in (parsed.get("tone_hints") or []) if str(t).strip()]
    directives = [str(d).strip() for d in (parsed.get("directives") or []) if str(d).strip()]
    override_raw = parsed.get("override_script")
    override = override_raw.strip() if isinstance(override_raw, str) and override_raw.strip() else None

    analysis = OpenCommentsAnalysis(
        mode=mode,
        raw_text=text,
        tone_hints=tone_hints,
        directives=directives,
        override_script=override,
        cost_usd=result.cost_usd,
    )

    # A bare pasted script (no marker) the LLM tagged OVERRIDE still gets the
    # same oversize flag + audit line as the marker path — one behaviour for
    # both, per the "marker + auto-detect" decision.
    if analysis.mode is OpenCommentsMode.OVERRIDE and analysis.override_script:
        analysis.override_oversize = (
            len(analysis.override_script.split()) > OVERRIDE_SOFT_MAX_WORDS
        )
        _audit_override(analysis, source="auto")

    _log.info(
        "classify_ok",
        mode=mode.value,
        tone_hint_count=len(tone_hints),
        directive_count=len(directives),
        has_override=override is not None,
        cost_usd=result.cost_usd,
    )
    return analysis
