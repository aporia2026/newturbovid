"""Script generator — produces the ~10-second VO script per row.

Routes the four Open Comments modes (plan §15 Appendix B):

  - ``OVERRIDE`` -> use the user's script verbatim, no LLM call
  - ``NONE``     -> article-only generation
  - ``TONE``     -> fold tone hints into the prompt
  - ``DIRECTIVE``-> enforce directives as MUST-include rules
  - ``MIXED``    -> apply tone hints AND enforce directives

Output:
  - ``script``: ready-to-speak text in the detected language (~20-24 words, ~10-12s)
  - ``style_direction``: short delivery hint for the TTS step (Gemini reads it)

Model: gpt-5.4-mini (Yoav directive, plan §5 "Models locked in").
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from bulkvid.adapters.openai_client import MODEL_SCRIPT_GEN, OpenAIClient
from bulkvid.logging import get_logger
from bulkvid.orchestrator.runtime_settings import (
    SCRIPT_SYSTEM_PROMPT_DEFAULT,
    SETTING_SCRIPT_SYSTEM_PROMPT,
)
from bulkvid.orchestrator.settings_store import SettingsStore
from bulkvid.pipeline.open_comments import OpenCommentsAnalysis, OpenCommentsMode

_log = get_logger("script")


# ── Constants ────────────────────────────────────────────────────────────────


# Target ~10-12 seconds of voiceover (video length == VO length). At the
# observed Gemini TTS rate (~2 words/sec) that's ~20-24 words; hard cap 26.
DEFAULT_TARGET_WORDS = 17
MIN_WORDS = 12
MAX_WORDS = 20
ARTICLE_PROMPT_CHARS = 3_000
DEFAULT_STYLE_DIRECTION = "Read warmly and clearly, like a friendly podcast host."


@dataclass
class ScriptResult:
    script: str
    style_direction: str
    language: str
    word_count: int
    cost_usd: float
    used_override: bool


# The full default lives in runtime_settings.py so the admin panel can edit it
# without a code change. SCRIPT_SYSTEM_PROMPT_DEFAULT is re-exported here for
# backward compatibility with any caller that imports it from this module.
SYSTEM_PROMPT_TEMPLATE = SCRIPT_SYSTEM_PROMPT_DEFAULT


def _substitute(template: str, **vars: object) -> str:
    """Format the template, treating any unknown ``{...}`` as a literal brace.

    Admin-edited prompts can drop placeholders or add their own examples —
    we don't want a missing key to crash the whole row. KeyError falls through
    to leaving the literal text in place.
    """
    try:
        return template.format(**vars)
    except (KeyError, IndexError) as e:
        _log.warning(
            "script_prompt_substitution_warning",
            error=str(e),
            note="missing placeholder; using a literal fallback",
        )
        # Do a one-by-one substitution that ignores unknowns.
        out = template
        for k, v in vars.items():
            out = out.replace("{" + k + "}", str(v))
        return out


def _format_system_prompt(
    template: str,
    language: str,
    country: str,
    vertical: str,
    script_pattern: str,
    target_words: int,
) -> str:
    return _substitute(
        template,
        language=language or "en",
        country=country.strip() or "the target market",
        vertical=vertical.strip() or "general",
        script_pattern=(script_pattern.strip() or "natural conversational opener"),
        target_words=target_words,
        min_words=MIN_WORDS,
        max_words=MAX_WORDS,
    )


def _format_user_message(
    article_body: str,
    open_comments: OpenCommentsAnalysis,
) -> str:
    parts: list[str] = []

    if open_comments.tone_hints:
        parts.append("TONE_HINTS: " + "; ".join(open_comments.tone_hints))
    if open_comments.directives:
        parts.append("DIRECTIVES (must honor each):")
        for d in open_comments.directives:
            parts.append(f"  - {d}")

    snippet = (article_body or "").strip()[:ARTICLE_PROMPT_CHARS]
    if snippet:
        parts.append("ARTICLE BODY:")
        parts.append(snippet)
    else:
        parts.append("ARTICLE BODY: (none provided — invent a generic intro for the vertical)")

    return "\n\n".join(parts)


def _word_count(text: str) -> int:
    return len([w for w in text.split() if w.strip()])


# ── Public API ───────────────────────────────────────────────────────────────


async def generate_script(
    client: OpenAIClient,
    *,
    article_body: str,
    country: str,
    vertical: str,
    language: str,
    script_pattern: str,
    open_comments: OpenCommentsAnalysis,
    target_words: int = DEFAULT_TARGET_WORDS,
    model: str = MODEL_SCRIPT_GEN,
    settings_store: SettingsStore | None = None,
) -> ScriptResult:
    """Generate (or pass through) a ~10-second VO script.

    Mode ``OVERRIDE`` short-circuits and uses the user's script verbatim —
    no LLM call, zero cost. Every other mode runs gpt-5.4-mini in JSON mode.

    When ``settings_store`` is provided, the system-prompt template is read
    from it (``script_system_prompt`` key) — this is what makes the prompt
    admin-editable without a redeploy.
    """
    # Mode OVERRIDE: use the user's text verbatim. Highest-priority signal.
    if open_comments.mode is OpenCommentsMode.OVERRIDE and open_comments.override_script:
        script = open_comments.override_script.strip()
        wc = _word_count(script)
        _log.info(
            "script_override_used",
            language=language,
            word_count=wc,
            cost_usd=0.0,
        )
        return ScriptResult(
            script=script,
            style_direction=DEFAULT_STYLE_DIRECTION,
            language=language,
            word_count=wc,
            cost_usd=0.0,
            used_override=True,
        )

    template = SYSTEM_PROMPT_TEMPLATE
    if settings_store is not None:
        template = await settings_store.get(
            SETTING_SCRIPT_SYSTEM_PROMPT, default=SYSTEM_PROMPT_TEMPLATE
        )

    system = _format_system_prompt(
        template,
        language=language,
        country=country,
        vertical=vertical,
        script_pattern=script_pattern,
        target_words=target_words,
    )

    # When the bulk team typed DIRECTIVES into Open Comments, they're explicit
    # per-row overrides. They MUST be honored even if they conflict with the
    # default prompt's compliance rules (e.g. "no CTAs"). Append an explicit
    # clause so the model knows which to prefer.
    if open_comments.directives:
        system += (
            "\n\n—————\nDIRECTIVES (override the rules above where they conflict):\n"
            "Any DIRECTIVES listed in the user message below are non-negotiable. "
            "You MUST include every directive in the final script verbatim where "
            "feasible, even if it conflicts with the compliance rules."
        )
    user = _format_user_message(article_body, open_comments)

    _log.info(
        "script_submit",
        language=language,
        country=country[:40],
        vertical=vertical[:40],
        mode=open_comments.mode.value,
        tone_hint_count=len(open_comments.tone_hints),
        directive_count=len(open_comments.directives),
        article_chars=min(len(article_body or ""), ARTICLE_PROMPT_CHARS),
    )

    result = await client.chat(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_tokens=600,
        temperature=0.7,
    )

    try:
        parsed = json.loads(result.text)
    except json.JSONDecodeError as e:
        _log.error("script_parse_failed", error=str(e), raw_preview=result.text[:200])
        # Salvage: treat raw text as the script. Better than failing the row.
        script_text = result.text.strip()
        return ScriptResult(
            script=script_text,
            style_direction=DEFAULT_STYLE_DIRECTION,
            language=language,
            word_count=_word_count(script_text),
            cost_usd=result.cost_usd,
            used_override=False,
        )

    script = str(parsed.get("script") or "").strip()
    style = str(parsed.get("style_direction") or "").strip() or DEFAULT_STYLE_DIRECTION

    if not script:
        # Model produced empty script. Fall back to a generic line so the row
        # still ships rather than blocking the whole batch.
        _log.warning("script_empty_response", parsed_keys=list(parsed.keys()))
        script = f"Discover more in our {vertical or 'latest'} update — see the link below."

    wc = _word_count(script)
    _log.info(
        "script_ok",
        language=language,
        word_count=wc,
        cost_usd=result.cost_usd,
        style_chars=len(style),
    )

    return ScriptResult(
        script=script,
        style_direction=style,
        language=language,
        word_count=wc,
        cost_usd=result.cost_usd,
        used_override=False,
    )
