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

    _log.info(
        "classify_ok",
        mode=mode.value,
        tone_hint_count=len(tone_hints),
        directive_count=len(directives),
        has_override=override is not None,
        cost_usd=result.cost_usd,
    )
    return analysis
