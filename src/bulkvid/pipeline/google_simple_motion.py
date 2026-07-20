"""Localized fixed-script generator for the ``google-simple-motion`` tab.

Unlike the article-driven planner (which writes a fresh voiceover), this tab
speaks a FIXED two-sentence template, translated into the article's language:

    "<Explore more | Learn more | Read more> about the <SUBJECT>.
     Discover key details and useful information about <SUBJECT>."

The opening is chosen at random per row (in the processor). ``<SUBJECT>`` is a
short, accurate description of the article's main topic. One cheap gpt call does
the subject extraction AND the localization together, emitting the two COMPLETE
grammatical sentences per language — a string-slotted subject would be
ungrammatical in inflected languages (German / Arabic / Polish case + gender), so
the model integrates it. The two sentences are TTS'd separately (a 3s silence is
spliced between them — see ``pipeline.audio_gap``), so we return them apart.

Model: gpt-5.4-mini (cheap, deterministic at low temperature). Plan
``_plans/2026-07-20-google-simple-motion-tab.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from bulkvid.adapters.openai_client import MODEL_SCRIPT_GEN, OpenAIClient
from bulkvid.logging import get_logger

_log = get_logger("gsmscript")

# The three fixed openings (English reference). The generator returns a localized
# variant for EACH; the processor picks one at random per row. Order is fixed so
# ``sentence1_variants[i]`` corresponds to ``OPENINGS[i]``.
OPENINGS: tuple[str, ...] = ("Explore more", "Learn more", "Read more")

# Article snippet cap fed to the extractor — enough to identify the topic without
# spending tokens on the whole body.
ARTICLE_PROMPT_CHARS = 2_000

_DEFAULT_SUBJECT = "this topic"


@dataclass
class LearnMoreScript:
    """One row's localized fixed script.

    ``sentence1_variants`` is length 3, aligned to :data:`OPENINGS`; the processor
    picks one at random. ``sentence2`` is the single closing line. Both are
    localized to the article language with the subject grammatically integrated.
    """

    subject: str
    sentence1_variants: list[str]
    sentence2: str
    cost_usd: float = 0.0


SYSTEM_PROMPT = """You localize a FIXED two-sentence ad voiceover for a short video.

You are given an article and a target LANGUAGE. Do TWO things:

1. Extract SUBJECT: a short, accurate noun phrase (3-6 words) naming the article's
   main topic, written in the target language. No full sentence, no punctuation.

2. Produce the fixed script in the target language, as TWO sentences. The template
   (English) is:
     Sentence 1: "<OPENING> about the <SUBJECT>."
     Sentence 2: "Discover key details and useful information about <SUBJECT>."
   Provide THREE variants of Sentence 1, one per opening, IN THIS ORDER:
     1) the variant for "Explore more"
     2) the variant for "Learn more"
     3) the variant for "Read more"
   Each variant must be ONE complete, natural, grammatical sentence in the target
   language, faithfully rendering the template with the SUBJECT named in full and
   correctly inflected. Do the same for Sentence 2 (one sentence).

Keep the wording faithful to the template — do NOT embellish, add claims, or change
the meaning. Every sentence ends with a period (or the language's sentence-ending
punctuation).

Return strict JSON with EXACTLY these fields:
{
  "subject": "...",
  "sentence1_variants": ["...", "...", "..."],
  "sentence2": "..."
}"""


def _english_fallback(subject: str = _DEFAULT_SUBJECT) -> LearnMoreScript:
    """A generic English script so a row still ships if localization fails."""
    subj = (subject or _DEFAULT_SUBJECT).strip() or _DEFAULT_SUBJECT
    return LearnMoreScript(
        subject=subj,
        sentence1_variants=[f"{opening} about {subj}." for opening in OPENINGS],
        sentence2=f"Discover key details and useful information about {subj}.",
    )


def _coerce(parsed: dict, *, cost_usd: float) -> LearnMoreScript:
    """Normalize the model's JSON into a ``LearnMoreScript``. Never raises.

    Missing / short / non-string fields degrade to the English fallback (subject
    is kept if usable) so a malformed response can't block the row.
    """
    subject = str(parsed.get("subject") or "").strip()
    raw_variants = parsed.get("sentence1_variants")
    variants = (
        [str(v).strip() for v in raw_variants if str(v).strip()]
        if isinstance(raw_variants, list)
        else []
    )
    sentence2 = str(parsed.get("sentence2") or "").strip()

    if not subject or len(variants) < 3 or not sentence2:
        _log.warning(
            "gsm_script_incomplete_fell_back",
            has_subject=bool(subject),
            variant_count=len(variants),
            has_sentence2=bool(sentence2),
        )
        fb = _english_fallback(subject)
        fb.cost_usd = cost_usd
        return fb

    return LearnMoreScript(
        subject=subject,
        sentence1_variants=variants[:3],
        sentence2=sentence2,
        cost_usd=cost_usd,
    )


async def generate_learn_more_script(
    client: OpenAIClient,
    *,
    article_body: str,
    language: str,
    model: str = MODEL_SCRIPT_GEN,
) -> LearnMoreScript:
    """Localize the fixed two-sentence script for one article. Never raises.

    Returns the subject, three localized opening variants (aligned to
    :data:`OPENINGS`), and the localized closing sentence. On any LLM / parse
    failure, degrades to a generic English script so the row still ships.
    """
    snippet = (article_body or "").strip()[:ARTICLE_PROMPT_CHARS]
    user = (
        f"TARGET LANGUAGE: {language or 'the article language'}\n\n"
        f"ARTICLE:\n{snippet}"
        if snippet
        else (
            f"TARGET LANGUAGE: {language or 'the article language'}\n\n"
            "ARTICLE: (none provided — use a generic, on-topic subject)"
        )
    )

    _log.info("gsm_script_submit", language=language, article_chars=len(snippet))

    try:
        result = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            max_tokens=400,
            temperature=0.2,
        )
    except Exception as e:    # never block the row on an LLM failure
        _log.error("gsm_script_llm_failed", error=str(e)[:200])
        return _english_fallback()

    try:
        parsed = json.loads(result.text)
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        _log.error(
            "gsm_script_parse_failed", error=str(e), raw_preview=result.text[:200]
        )
        fb = _english_fallback()
        fb.cost_usd = result.cost_usd
        return fb

    script = _coerce(parsed, cost_usd=result.cost_usd)
    _log.info(
        "gsm_script_ok",
        language=language,
        subject_words=len(script.subject.split()),
        cost_usd=script.cost_usd,
    )
    return script
