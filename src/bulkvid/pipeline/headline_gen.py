"""Generate a punchy card-headline from an article body.

Used ONLY by the simple x4 card-overlay path — when at least one of a row's
4 ``Template*`` cells is non-blank, the renderer needs a string to draw at
the top of the card. The script generator already produces a voiceover
script, but a script is the wrong shape for a card headline (16-18 word
neutral narration vs. ~6-word marketing hook). One small extra GPT call
is the cleanest source.

Returns ``(text, cost_usd)`` matching the convention of every other pipeline
call. Failure is non-fatal — the caller falls back to an empty headline,
which the renderer simply skips (the card still works, it just has no title).

Plan: ``_plans/2026-06-08-simple-x4-template-cards.md`` §D.4 (Headline source).
"""

from __future__ import annotations

from bulkvid.adapters.openai_client import MODEL_SCRIPT_GEN, OpenAIClient
from bulkvid.logging import get_logger

_log = get_logger("headline_gen")


# ── Prompt ──────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You write very short marketing headlines (max 8 words) for short-form "
    "video ad cards. You only return the headline text itself — no quotes, "
    "no preamble, no trailing punctuation, no commentary."
)


def _user_message(*, article_excerpt: str, language: str, vertical: str) -> str:
    snippet = (article_excerpt or "").strip()[:1200]
    return (
        f"Language: {language or 'English'}\n"
        f"Vertical: {vertical or '(unknown)'}\n\n"
        f"Article excerpt:\n{snippet or '(no article body)'}\n\n"
        "Produce ONE headline in the same language. Maximum 8 words. "
        "Direct and concrete; no questions, no emojis, no clickbait. "
        "Return ONLY the headline."
    )


# ── Public API ──────────────────────────────────────────────────────────────


async def generate_card_headline(
    client: OpenAIClient,
    *,
    article_excerpt: str,
    language: str,
    vertical: str,
    model: str = MODEL_SCRIPT_GEN,
    max_words: int = 12,
) -> tuple[str, float]:
    """Generate a card headline. Returns ``(text, cost_usd)``.

    Soft-fail: any exception in the GPT call yields ``("", 0.0)`` so the
    caller can render the card without a title rather than failing the row.
    Token budget is small (~50 input + ~20 output) → tiny cost.

    ``max_words`` is a defense-in-depth clamp after the model returns: the
    prompt asks for ≤8 words but we accept up to ``max_words`` before
    truncating, so a borderline ninth word doesn't get silently dropped.
    """
    try:
        result = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _user_message(
                        article_excerpt=article_excerpt,
                        language=language,
                        vertical=vertical,
                    ),
                },
            ],
            max_tokens=40,
            temperature=0.4,
        )
    except Exception as e:    # never fail the row over a headline
        _log.warning(
            "headline_gen_failed",
            error=type(e).__name__,
            detail=str(e)[:200],
        )
        return ("", 0.0)

    text = (result.text or "").strip().strip('"').strip("'").strip()
    # Strip a single trailing period — common GPT habit, looks wrong on a card.
    if text.endswith("."):
        text = text[:-1].rstrip()

    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words])

    _log.info(
        "headline_gen_ok",
        chars=len(text),
        words=len(text.split()),
        cost_usd=result.cost_usd,
    )
    return (text, result.cost_usd)
