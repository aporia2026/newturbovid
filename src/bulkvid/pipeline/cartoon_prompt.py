"""Cartoon-mode planner — turns an article into animated video ideas.

One gpt-5.4-mini call (JSON mode) produces ``num_ideas`` independent video ideas
from the article. Each idea is a tiny multi-shot story:

  - ``voiceover``        — one short line (~6-7s spoken) in the detected language
  - ``style_direction``  — delivery hint for the Gemini TTS step
  - ``shots``            — ``num_shots`` scenes, each a SCENE description (for the
                           nano-banana-2 image step) plus a MOTION description
                           (for the Seedance image-to-video step)

Hard rules baked into the system prompt (plan §"Security & safety"):
  - GENERIC / SYMBOLIC characters only — never a real, named person's likeness.
  - One recurring character per idea, described consistently across its shots so
    the image-to-image chaining holds the look across the cut.
  - NO legible on-screen text (screens, signs, captions) — keep them abstract.

The shared cartoon STYLE preamble is composed onto each scene by the row
processor (``image_prompt_for_shot``), not here, so the planner stays focused on
content.

Model: gpt-5.4-mini (Yoav directive, plan §5 "Models locked in").
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from bulkvid.adapters.openai_client import MODEL_SCRIPT_GEN, OpenAIClient
from bulkvid.logging import get_logger
from bulkvid.orchestrator.runtime_settings import (
    CARTOON_PLANNER_PROMPT_DEFAULT,
    SETTING_CARTOON_PLANNER_PROMPT,
    SETTING_SCRIPT_TEMPLATE_LIBRARY,
    SETTING_SENSITIVE_APPAREL_RULES,
    SETTING_TEMPLATE_SELECTOR_ENABLED,
)
from bulkvid.orchestrator.settings_store import SettingsStore
from bulkvid.pipeline.open_comments import OpenCommentsAnalysis
from bulkvid.pipeline.safety import SAFE, SafetyContext, append_safety_block
from bulkvid.pipeline.template_selector import (
    TemplateLibraryParseError,
    parse_library,
    select_default_template,
)

_log = get_logger("cartoonprompt")


# ── Constants ────────────────────────────────────────────────────────────────

# Validated cartoon look (spike 2026-06-03): warm, flat, semi-realistic digital
# cartoon illustration. Prepended to every scene prompt so all shots share it.
CARTOON_STYLE = (
    "Flat semi-realistic digital cartoon illustration, warm soft lighting, "
    "clean confident linework, gentle painterly shading, vibrant but natural "
    "colors, modern 2D animated-film look."
)

# Each voiceover targets ~5-6 seconds of speech, sized so that even at the slow
# end of the Gemini TTS rate (~1.5 wps observed after the 1.3x speed-up), the
# full line fits inside the cartoon row processor's hard 8.0s video ceiling
# with a 0.5s trailing dwell. At slow delivery: 12 words / 1.5 wps = 8s raw,
# 6.15s effective after the 1.3x speed-up — leaves ~1.4s of margin under the
# 7.5s MAX_EFFECTIVE_VO_SECONDS. At fast delivery: 12 words / 3.5 wps = 3.4s
# raw, 2.6s effective — short, but the row processor pads the video to a flat
# 8s regardless of VO length, so there's no dead air pressure to push higher.
# Anything that still comes out > 7.5s effective triggers the row processor's
# shorten-and-retry path (see _plans/2026-06-04-cartoon-8s-hard-cap.md).
CARTOON_TARGET_WORDS = 10
CARTOON_MIN_WORDS = 8
CARTOON_MAX_WORDS = 12

DEFAULT_NUM_IDEAS = 2
DEFAULT_NUM_SHOTS = 2
ARTICLE_PROMPT_CHARS = 3_000
DEFAULT_STYLE_DIRECTION = "Read warmly and clearly, like a friendly podcast host."

# Brand-safety clause appended to EVERY image prompt. The image model renders
# recognizable brands by default (e.g. a "car" comes out as a badged VW with a
# readable plate), so this MUST go in the prompt the model actually sees — a
# planner-only rule is not enough. Crucial: no real logos or brands ever.
NO_BRANDING = (
    "Show only generic, unbranded vehicles, products, and signage — absolutely no "
    "real brand names, manufacturer logos, badges, emblems, or hood ornaments, and "
    "no readable license-plate text (leave any plates blank)."
)

# Consistency clause appended to chained (shot 2+) image prompts so the
# image-to-image step keeps the same character and look as the first shot.
CONSISTENCY_CLAUSE = (
    "Keep the SAME main character, outfit, and art style as the reference image."
)


@dataclass
class CartoonShot:
    scene: str          # what the scene shows (no style preamble)
    motion: str         # how it animates (Seedance motion prompt)


@dataclass
class CartoonIdea:
    voiceover: str
    style_direction: str
    shots: list[CartoonShot] = field(default_factory=list)


@dataclass
class CartoonPlan:
    ideas: list[CartoonIdea]
    cost_usd: float
    # Set when the blank-cell template selector picked a library entry for
    # this row. Empty when script_pattern was non-blank or the selector
    # didn't run. Surfaced to the sidebar so operators see which seed was
    # used. Plan §B.
    chosen_template_id: str = ""


@dataclass
class ShortenResult:
    voiceover: str
    cost_usd: float


# ── Prompt construction ──────────────────────────────────────────────────────


def _format_planner_prompt(
    template: str, *, language: str, num_ideas: int, num_shots: int
) -> str:
    """Substitute the planner's per-row placeholders into the admin template.

    Tolerant of unknown ``{...}`` tokens — admin edits may drop a placeholder
    or paste literal braces in examples. KeyError falls through to a one-by-one
    replacement that leaves unknown tokens as-is.
    """
    vars: dict[str, object] = {
        "language": language or "the article language",
        "num_ideas": num_ideas,
        "num_shots": num_shots,
        "target_words": CARTOON_TARGET_WORDS,
        "min_words": CARTOON_MIN_WORDS,
        "max_words": CARTOON_MAX_WORDS,
    }
    try:
        return template.format(**vars)
    except (KeyError, IndexError) as e:
        _log.warning(
            "cartoon_planner_substitution_warning",
            error=str(e),
            note="missing placeholder; using literal fallback",
        )
        out = template
        for k, v in vars.items():
            out = out.replace("{" + k + "}", str(v))
        return out


def _user_message(
    article_body: str, open_comments: OpenCommentsAnalysis
) -> str:
    parts: list[str] = []
    if open_comments.tone_hints:
        parts.append("TONE_HINTS: " + "; ".join(open_comments.tone_hints))
    if open_comments.directives:
        parts.append("DIRECTIVES (honor each): " + "; ".join(open_comments.directives))
    if open_comments.override_script:
        parts.append(
            "PREFERRED VOICEOVER (use or adapt for at least one idea):\n"
            + open_comments.override_script.strip()
        )

    snippet = (article_body or "").strip()[:ARTICLE_PROMPT_CHARS]
    if snippet:
        parts.append("ARTICLE BODY:\n" + snippet)
    else:
        parts.append(
            "ARTICLE BODY: (none provided — invent a generic, on-topic concept)"
        )
    return "\n\n".join(parts)


def image_prompt_for_shot(scene: str, *, is_chained: bool) -> str:
    """Compose the full nano-banana-2 prompt for one shot.

    Prepends the shared cartoon STYLE; for chained (shot 2+) shots, appends the
    consistency clause so the image-to-image step holds the character.
    """
    base = f"{CARTOON_STYLE} {scene.strip()} {NO_BRANDING}"
    return f"{base} {CONSISTENCY_CLAUSE}" if is_chained else base


# ── Fallback ─────────────────────────────────────────────────────────────────


def _fallback_plan(vertical: str, num_ideas: int, num_shots: int) -> list[CartoonIdea]:
    """A generic, on-topic plan so a row still ships if the LLM output is unusable."""
    topic = (vertical or "the topic").strip() or "the topic"
    ideas: list[CartoonIdea] = []
    for _ in range(num_ideas):
        shots = [
            CartoonShot(
                scene=(
                    f"A friendly cartoon character looking thoughtful while "
                    f"considering {topic}, warm everyday setting."
                ),
                motion="Subtle natural movement and a slow, gentle camera push-in.",
            )
            for _ in range(num_shots)
        ]
        ideas.append(
            CartoonIdea(
                voiceover=f"Here's what you should know about {topic} today.",
                style_direction=DEFAULT_STYLE_DIRECTION,
                shots=shots,
            )
        )
    return ideas


def _enforce_word_cap(text: str, max_words: int) -> str:
    """Truncate ``text`` to at most ``max_words`` words.

    Returns the original (stripped) text when already within the cap. When
    truncation is needed, prefers to end at the last sentence boundary
    (``.``, ``!``, ``?``) inside the cap so the spoken line doesn't end
    mid-thought.

    When no real sentence boundary exists within the cap, returns the empty
    string — signalling "this VO is incomplete, drop the idea". The previous
    behaviour was to chop at the word boundary and append an artificial
    period, which produced grammatically-terminated but semantically
    incomplete VOs ("...and." / "...with."). See
    ``refs/cartoon-debug/v1.mp4`` for an example of the failure mode and
    the 2026-06-04 fragment-debug session for the diagnosis.

    Backstop for model drift past the prompt's word range — see
    ``CARTOON_MAX_WORDS`` rationale above.
    """
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    truncated = " ".join(words[:max_words])
    half = len(truncated) // 2
    last_break = max(truncated.rfind(p) for p in ".!?")
    if last_break >= half:
        return truncated[: last_break + 1].rstrip()
    return ""


def _first_nonempty(d: dict, *keys: str) -> str:
    """Return the first present-and-nonblank string value across ``keys``.

    Lets the coercer accept shape drift from the model (``voice_over`` instead
    of ``voiceover``, ``description`` instead of ``scene``, etc.) without
    rewriting the same defensive lookup at every call site.
    """
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


_DEFAULT_MOTION = "Subtle, natural movement."


@dataclass
class _DroppedFragment:
    """A planner idea whose VO didn't end on a sentence boundary but whose
    shots / style are otherwise valid. The caller (``generate_cartoon_plan``)
    can attempt to recover it via ``complete_voiceover``; if that fails too,
    the existing fallback-padding kicks in."""

    voiceover_raw: str        # the original line that failed sentence-boundary validation
    style: str
    shots: list[CartoonShot]


def _coerce_ideas(
    raw_ideas: list[Any], num_ideas: int, num_shots: int
) -> tuple[list[CartoonIdea], list[_DroppedFragment]]:
    """Normalize parsed JSON into ``num_ideas`` ideas of ``num_shots`` shots.

    Returns ``(kept, fragments)``. ``kept`` is the list of ideas that passed
    every validation step. ``fragments`` is the list of ideas whose shots/style
    parsed cleanly but whose VO didn't end on a sentence boundary — caller
    can try ``complete_voiceover`` to recover them before falling back to
    generic padding.

    Defensive against the gpt-5.4-mini planner's shape drift (live runs have
    seen alternate key names and short/long shot lists):
      - voiceover may be under voiceover / voice_over / vo / line / script /
        narration
      - shots may be under shots / scenes / sequence (and shots can be bare
        strings instead of {scene, motion} dicts)
      - scene may be under scene / description / visual / image / prompt
      - motion may be under motion / action / animation / movement
      - shot lists too short are padded by repeating the last valid shot
      - shot lists too long are trimmed to ``num_shots``

    Anything that can't yield BOTH a non-empty voiceover_raw AND at least one
    valid scene is dropped silently with a debug log — there's nothing to
    recover. Ideas whose only problem is an incomplete-sentence VO are
    surfaced as ``_DroppedFragment``s for the caller to retry.
    """
    kept: list[CartoonIdea] = []
    fragments: list[_DroppedFragment] = []
    for raw_idx, raw in enumerate(raw_ideas[:num_ideas]):
        if not isinstance(raw, dict):
            _log.debug(
                "cartoon_idea_rejected",
                idea_index=raw_idx, reason="not_a_dict",
                type=type(raw).__name__,
            )
            continue
        voiceover_raw = _first_nonempty(
            raw, "voiceover", "voice_over", "vo", "line", "script", "narration"
        )
        if not voiceover_raw:
            _log.debug(
                "cartoon_idea_rejected",
                idea_index=raw_idx, reason="no_voiceover",
                keys=list(raw.keys())[:10],
            )
            continue
        original_words = len(voiceover_raw.split())
        voiceover = _enforce_word_cap(voiceover_raw, CARTOON_MAX_WORDS)
        if original_words > CARTOON_MAX_WORDS:
            _log.warning(
                "cartoon_voiceover_capped",
                idea_index=raw_idx,
                original_words=original_words,
                max_words=CARTOON_MAX_WORDS,
                capped_words=len(voiceover.split()),
            )

        # Parse shots + style BEFORE the VO sentence-boundary check, so a
        # fragment idea can still be surfaced for recovery (we need shots /
        # style to rebuild the idea after a successful rewrite).
        style = (
            _first_nonempty(raw, "style_direction", "style", "tone", "delivery")
            or DEFAULT_STYLE_DIRECTION
        )

        raw_shots = raw.get("shots") or raw.get("scenes") or raw.get("sequence") or []
        if not isinstance(raw_shots, list):
            raw_shots = []
        shots: list[CartoonShot] = []
        for rs in raw_shots:
            if isinstance(rs, str):
                scene = rs.strip()
                if scene:
                    shots.append(CartoonShot(scene=scene, motion=_DEFAULT_MOTION))
                continue
            if not isinstance(rs, dict):
                continue
            scene = _first_nonempty(rs, "scene", "description", "visual", "image", "prompt")
            motion = (
                _first_nonempty(rs, "motion", "action", "animation", "movement")
                or _DEFAULT_MOTION
            )
            if scene:
                shots.append(CartoonShot(scene=scene, motion=motion))

        if not shots:
            _log.debug(
                "cartoon_idea_rejected",
                idea_index=raw_idx, reason="no_valid_shots",
                keys=list(raw.keys())[:10],
            )
            continue
        # Pad short lists by repeating the last valid shot (image-to-image
        # chaining keeps the visual cohesive even when the scenes are similar),
        # and trim long lists down to the requested count.
        while len(shots) < num_shots:
            shots.append(shots[-1])
        shots = shots[:num_shots]

        # Final gate: the VO must end on a real sentence boundary. If it
        # doesn't, surface the idea as a fragment so the caller can try to
        # recover it rather than silently losing the shots+style work.
        if not voiceover or not voiceover.rstrip().endswith((".", "!", "?")):
            _log.warning(
                "cartoon_idea_fragment_for_recovery",
                idea_index=raw_idx,
                original_words=original_words,
                voiceover_preview=voiceover_raw[:120],
            )
            fragments.append(
                _DroppedFragment(
                    voiceover_raw=voiceover_raw,
                    style=style,
                    shots=shots,
                )
            )
            continue
        kept.append(CartoonIdea(voiceover=voiceover, style_direction=style, shots=shots))
    return kept, fragments


# ── Public API ───────────────────────────────────────────────────────────────


async def _maybe_select_cartoon_template(
    client: OpenAIClient,
    *,
    settings_store: SettingsStore,
    vertical: str,
    country: str,
    article_body: str,
    safety: SafetyContext,
):    # -> Template | None
    """Cartoon-side wrapper around the shared template selector.

    Mirrors ``script_gen._maybe_select_template`` so cartoon and script tabs
    share the same library + enable-switch semantics. Any failure path
    returns ``None`` and the planner proceeds without a template.
    """
    enabled_raw = await settings_store.get(
        SETTING_TEMPLATE_SELECTOR_ENABLED, default="true"
    )
    if enabled_raw.strip().lower() not in {"true", "1", "yes", "on"}:
        _log.info("template_selector_disabled", surface="cartoon")
        return None

    library_raw = await settings_store.get(
        SETTING_SCRIPT_TEMPLATE_LIBRARY, default=""
    )
    try:
        library = parse_library(library_raw)
    except TemplateLibraryParseError as e:
        _log.warning("template_library_parse_failed", surface="cartoon", error=str(e))
        return None

    if not library.enabled_templates():
        _log.info("template_library_empty", surface="cartoon")
        return None

    body = (article_body or "").strip()
    first_line = body.splitlines()[0].strip() if body else ""
    article_title = first_line[:200]
    article_excerpt = body[:600]

    return await select_default_template(
        client,
        library=library,
        vertical=vertical,
        country=country,
        article_title=article_title,
        article_excerpt=article_excerpt,
        safety=safety,
    )


async def generate_cartoon_plan(
    client: OpenAIClient,
    *,
    article_body: str,
    country: str,
    vertical: str,
    language: str,
    script_pattern: str,
    open_comments: OpenCommentsAnalysis,
    num_ideas: int = DEFAULT_NUM_IDEAS,
    num_shots: int = DEFAULT_NUM_SHOTS,
    model: str = MODEL_SCRIPT_GEN,
    settings_store: SettingsStore | None = None,
    safety: SafetyContext = SAFE,
) -> CartoonPlan:
    """Plan ``num_ideas`` cartoon videos from the article. Never raises.

    On any parse/shape problem it falls back to a generic on-topic plan so the
    row still ships rather than blocking the batch.

    The planner system prompt is admin-editable via the ``cartoon_planner_prompt``
    setting; when ``safety.matched`` the sensitive-apparel block is appended
    so both the voiceover and the scene descriptions stay product-only.
    """
    template = CARTOON_PLANNER_PROMPT_DEFAULT
    safety_block = ""
    if settings_store is not None:
        template = await settings_store.get(
            SETTING_CARTOON_PLANNER_PROMPT, default=CARTOON_PLANNER_PROMPT_DEFAULT
        )
        if safety.matched:
            # No explicit default — let the store fall through to the
            # registered default (``SENSITIVE_APPAREL_RULES_DEFAULT``).
            safety_block = await settings_store.get(
                SETTING_SENSITIVE_APPAREL_RULES
            )

    system = _format_planner_prompt(
        template,
        language=language,
        num_ideas=num_ideas,
        num_shots=num_shots,
    )
    # Blank script_pattern → ask the selector for a default template body.
    # Cartoon shares the same library as the script tabs (Yoav 2026-06-07,
    # answer 3 in the open-questions block).
    effective_pattern = script_pattern.strip()
    chosen_template_id = ""
    if not effective_pattern and settings_store is not None:
        chosen = await _maybe_select_cartoon_template(
            client,
            settings_store=settings_store,
            vertical=vertical,
            country=country,
            article_body=article_body,
            safety=safety,
        )
        if chosen is not None:
            effective_pattern = chosen.body.strip()
            chosen_template_id = chosen.id
    if effective_pattern:
        system += f"\n\nPreferred opening style: {effective_pattern}."
    system = append_safety_block(system, safety, safety_block)
    if safety.matched:
        _log.info(
            "safety_applied",
            stage="cartoon_planner_prompt",
            matched_keyword=safety.matched_keyword,
        )
    user = _user_message(article_body, open_comments)

    _log.info(
        "cartoon_plan_submit",
        language=language,
        country=country[:40],
        vertical=vertical[:40],
        num_ideas=num_ideas,
        num_shots=num_shots,
        article_chars=min(len(article_body or ""), ARTICLE_PROMPT_CHARS),
    )

    result = await client.chat(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_tokens=1200,
        # 0.5 (was 0.8). Lower temperature pushes the planner toward more
        # conventional sentence endings — fewer "independent" / "different"
        # bare-adjective closings that pass the grammar gate but sound
        # unfinished. Two ideas per row still get enough variety.
        temperature=0.5,
    )

    ideas: list[CartoonIdea] = []
    fragments: list[_DroppedFragment] = []
    try:
        parsed = json.loads(result.text)
        ideas, fragments = _coerce_ideas(
            parsed.get("ideas") or [], num_ideas, num_shots
        )
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        _log.error("cartoon_plan_parse_failed", error=str(e), raw_preview=result.text[:200])

    # If we have fragments AND we're short on ideas, try to recover them by
    # asking the model to rewrite the line as a complete sentence. One LLM
    # call per fragment (~$0.001), no Seedance / TTS / Rendi cost. The recovery
    # only runs as long as we still need ideas — once we hit ``num_ideas`` we
    # stop (no point spending tokens on extras the row won't ship).
    total_recovery_cost = 0.0
    recovered_count = 0
    for frag in fragments:
        if len(ideas) >= num_ideas:
            break
        try:
            rewrite = await complete_voiceover(
                client,
                text=frag.voiceover_raw,
                language=language,
                target_words=CARTOON_TARGET_WORDS,
            )
        except Exception as e:    # last-line defense — recovery is best-effort
            _log.error(
                "cartoon_fragment_recovery_failed",
                voiceover_preview=frag.voiceover_raw[:80],
                error=str(e)[:200],
            )
            continue
        total_recovery_cost += rewrite.cost_usd
        # complete_voiceover returns the original text on any failure
        # (parse, empty, still-fragment). Same string out as in means recovery
        # didn't take.
        recovered = rewrite.voiceover.strip()
        if (
            recovered
            and recovered != frag.voiceover_raw
            and recovered.rstrip().endswith((".", "!", "?"))
        ):
            ideas.append(
                CartoonIdea(
                    voiceover=recovered,
                    style_direction=frag.style,
                    shots=frag.shots,
                )
            )
            recovered_count += 1
            _log.info(
                "cartoon_fragment_recovered",
                original_words=len(frag.voiceover_raw.split()),
                rewrite_words=len(recovered.split()),
            )
        else:
            _log.warning(
                "cartoon_fragment_unrecoverable",
                voiceover_preview=frag.voiceover_raw[:120],
            )

    if len(ideas) < num_ideas:
        _log.warning(
            "cartoon_plan_incomplete_filled",
            got=len(ideas),
            wanted=num_ideas,
            fragments_seen=len(fragments),
            fragments_recovered=recovered_count,
            raw_preview=result.text[:300],
        )
        ideas += _fallback_plan(vertical, num_ideas - len(ideas), num_shots)

    _log.info(
        "cartoon_plan_ok",
        idea_count=len(ideas),
        fragments_seen=len(fragments),
        fragments_recovered=recovered_count,
        cost_usd=result.cost_usd + total_recovery_cost,
    )
    return CartoonPlan(
        ideas=ideas,
        cost_usd=result.cost_usd + total_recovery_cost,
        chosen_template_id=chosen_template_id,
    )


# ── Complete a fragment VO (used when the planner returns a mid-thought line)


async def complete_voiceover(
    client: OpenAIClient,
    *,
    text: str,
    language: str,
    target_words: int,
    model: str = MODEL_SCRIPT_GEN,
) -> ShortenResult:
    """Rewrite a planner-produced VO fragment into a complete sentence.

    Used by ``generate_cartoon_plan`` when ``_coerce_ideas`` finds a VO that
    doesn't end on a sentence boundary (period, question mark, exclamation).
    Instead of dropping the idea, we give the LLM ONE chance to rewrite the
    line cleanly while preserving the meaning and staying within the word
    budget.

    Differs from ``shorten_voiceover`` only in framing: there the input is
    "too long, shorten it"; here the input is "incomplete, finish it". The
    rewrite may legitimately be the same length or even slightly longer,
    so we do NOT apply the "must be shorter" check.

    Defensive: on parse failure, empty result, or a rewrite that still
    doesn't end on a sentence boundary, returns the original ``text``
    unchanged so the caller (the planner) knows to fall back to padding.
    """
    system = (
        f"You rewrite voiceover lines in {language}. The user's line is "
        "an incomplete fragment — it stops mid-thought (often on a "
        "conjunction or preposition like 'and', 'with', 'that'). "
        f"Rewrite it as ONE complete sentence in {target_words} words or "
        "fewer, preserving the meaning and the language. MUST end at a "
        "clean sentence boundary (period, question mark, or exclamation). "
        'Return JSON: {"voiceover": "..."}.'
    )
    user = text.strip()

    _log.info(
        "cartoon_complete_submit",
        target_words=target_words,
        original_words=len(text.split()),
    )

    result = await client.chat(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_tokens=200,
        temperature=0.3,
    )

    try:
        parsed = json.loads(result.text)
        new_vo = str(parsed.get("voiceover", "")).strip()
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        _log.warning(
            "cartoon_complete_parse_failed",
            error=str(e),
            raw_preview=result.text[:120],
        )
        return ShortenResult(voiceover=text, cost_usd=result.cost_usd)

    if not new_vo:
        _log.warning("cartoon_complete_empty_returned_original", original=text[:80])
        return ShortenResult(voiceover=text, cost_usd=result.cost_usd)

    # Cap to the word budget; if the cap returns "" the rewrite landed
    # mid-thought again — same outcome as a malformed shorten, drop signal.
    capped = _enforce_word_cap(new_vo, target_words)
    if not capped or not capped.rstrip().endswith((".", "!", "?")):
        _log.warning(
            "cartoon_complete_still_fragment_returned_original",
            original=text[:80],
            rewrite_preview=new_vo[:80],
            cap_result_preview=capped[:80],
        )
        return ShortenResult(voiceover=text, cost_usd=result.cost_usd)

    _log.info(
        "cartoon_complete_ok",
        original_words=len(text.split()),
        returned_words=len(capped.split()),
        cost_usd=result.cost_usd,
    )
    return ShortenResult(voiceover=capped, cost_usd=result.cost_usd)


# ── Shorten a single VO (used when the synthesized TTS overshoots 8s) ───────


async def shorten_voiceover(
    client: OpenAIClient,
    *,
    text: str,
    language: str,
    target_words: int,
    model: str = MODEL_SCRIPT_GEN,
) -> ShortenResult:
    """Rewrite ``text`` in fewer words while preserving meaning.

    Called from the cartoon row processor when the synthesized TTS for an idea
    measures effectively longer than ``MAX_EFFECTIVE_VO_SECONDS`` and would
    not fit inside the 8.0s video ceiling without truncation. One LLM call,
    JSON mode for robust parse.

    Defensive: on parse failure, empty result, or a "shorter" rewrite that's
    actually the same length or longer (model misbehaviour), returns the
    original ``text`` unchanged. The caller can then decide whether to drop
    the idea (see ``_plans/2026-06-04-cartoon-8s-hard-cap.md``).
    """
    system = (
        f"You rewrite voiceover lines in {language}. The user's line is too "
        f"long for a short video. Rewrite it in {target_words} words or fewer "
        "while preserving the meaning and the language. End at a clean "
        "sentence boundary (period, question mark, or exclamation). "
        'Return JSON: {"voiceover": "..."}.'
    )
    user = text.strip()

    _log.info(
        "cartoon_shorten_submit",
        target_words=target_words,
        original_words=len(text.split()),
    )

    result = await client.chat(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_tokens=200,
        temperature=0.3,
    )

    try:
        parsed = json.loads(result.text)
        new_vo = str(parsed.get("voiceover", "")).strip()
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        _log.warning(
            "cartoon_shorten_parse_failed",
            error=str(e),
            raw_preview=result.text[:120],
        )
        return ShortenResult(voiceover=text, cost_usd=result.cost_usd)

    if not new_vo:
        _log.warning("cartoon_shorten_empty_returned_original", original=text[:80])
        return ShortenResult(voiceover=text, cost_usd=result.cost_usd)

    # Backstop — a "shorter" rewrite that's actually the same length or longer
    # is a model misbehaviour. Fall back to the original so the caller's
    # decision (drop the idea) is based on the original measurement, not a
    # false-positive shortening.
    if len(new_vo.split()) >= len(text.split()):
        _log.warning(
            "cartoon_shorten_not_shorter_returned_original",
            original_words=len(text.split()),
            returned_words=len(new_vo.split()),
        )
        return ShortenResult(voiceover=text, cost_usd=result.cost_usd)

    # Hard cap to target_words in case the model went slightly over. The
    # cap returns "" when truncation lands mid-thought (no real sentence
    # boundary inside the cap). Either way, the result must end on .!? —
    # otherwise we'd ship a fragment, which is the bug this whole helper
    # exists to prevent. Fall back to the original on either failure so
    # the caller drops the idea (matches the existing "no change" path).
    capped = _enforce_word_cap(new_vo, target_words)
    if not capped or not capped.rstrip().endswith((".", "!", "?")):
        _log.warning(
            "cartoon_shorten_incomplete_returned_original",
            original=text[:80],
            shortened_preview=new_vo[:80],
            cap_result_preview=capped[:80],
        )
        return ShortenResult(voiceover=text, cost_usd=result.cost_usd)
    _log.info(
        "cartoon_shorten_ok",
        original_words=len(text.split()),
        returned_words=len(capped.split()),
        cost_usd=result.cost_usd,
    )
    return ShortenResult(voiceover=capped, cost_usd=result.cost_usd)
