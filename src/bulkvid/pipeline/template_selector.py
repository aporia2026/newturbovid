"""GPT-based selector for the default script template library.

When a row's ``script_pattern`` column is blank, the runner calls
``select_default_template`` with the admin-edited template library + per-row
context. The selector asks gpt-5.4-mini to pick the best template id and
returns the corresponding ``Template``.

Plan: ``_plans/2026-06-07-overload-handling-and-template-defaults.md`` §B.2.

Failure handling is opinionated: any anomaly (invalid JSON, hallucinated id,
exception during the call) returns ``None`` so the caller can fall back to
the existing literal default. The selector never raises into the row
processor — the row keeps moving.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from bulkvid.adapters.openai_client import MODEL_SCRIPT_GEN, OpenAIClient
from bulkvid.logging import get_logger
from bulkvid.pipeline.safety import SAFE, SafetyContext

_log = get_logger("template_selector")


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Template:
    """One library entry.

    ``body`` is what gets substituted into the ``{script_pattern}`` slot of the
    downstream script-generation prompt. ``hint`` is what the selector sees
    when asked to pick — keep it short and discriminative.

    ``match_hints`` are advisory only: they ride along with the hint into the
    selector prompt so GPT knows when a template tends to apply, but they
    do NOT hard-filter the candidates. GPT is allowed to override them when
    the article suggests otherwise.
    """

    id: str
    name: str
    hint: str
    body: str
    match_hints: dict[str, list[str]] = field(default_factory=dict)
    enabled: bool = True


@dataclass(frozen=True)
class TemplateLibrary:
    version: int
    templates: tuple[Template, ...]

    def enabled_templates(self) -> tuple[Template, ...]:
        return tuple(t for t in self.templates if t.enabled)

    def by_id(self, template_id: str) -> Template | None:
        for t in self.templates:
            if t.id == template_id and t.enabled:
                return t
        return None


# ── Parsing ─────────────────────────────────────────────────────────────────


class TemplateLibraryParseError(ValueError):
    """The library JSON didn't conform to the expected shape."""


def parse_library(raw: str) -> TemplateLibrary:
    """Parse the JSON blob stored under ``script_template_library``.

    Strict-ish: rejects missing required fields, accepts and ignores unknown
    ones (so the schema can grow without breaking older deploys).
    """
    if not raw or not raw.strip():
        return TemplateLibrary(version=1, templates=())
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise TemplateLibraryParseError(f"invalid JSON: {e}") from e
    if not isinstance(doc, dict):
        raise TemplateLibraryParseError("top-level value must be an object")
    version_raw = doc.get("version", 1)
    try:
        version = int(version_raw)
    except (TypeError, ValueError) as e:
        raise TemplateLibraryParseError(f"version must be an int: {version_raw!r}") from e
    templates_raw = doc.get("templates", [])
    if not isinstance(templates_raw, list):
        raise TemplateLibraryParseError("templates must be a list")

    parsed: list[Template] = []
    for i, entry in enumerate(templates_raw):
        if not isinstance(entry, dict):
            raise TemplateLibraryParseError(
                f"templates[{i}] must be an object, got {type(entry).__name__}"
            )
        try:
            tid = str(entry["id"]).strip()
            name = str(entry["name"]).strip()
            body = str(entry["body"])
        except KeyError as e:
            raise TemplateLibraryParseError(
                f"templates[{i}] missing required field {e}"
            ) from e
        if not tid:
            raise TemplateLibraryParseError(f"templates[{i}].id must be non-empty")
        hint = str(entry.get("hint", "")).strip()
        enabled = bool(entry.get("enabled", True))
        match_hints_raw = entry.get("match_hints") or {}
        if not isinstance(match_hints_raw, dict):
            raise TemplateLibraryParseError(
                f"templates[{i}].match_hints must be an object"
            )
        match_hints: dict[str, list[str]] = {}
        for k, v in match_hints_raw.items():
            if not isinstance(v, list):
                raise TemplateLibraryParseError(
                    f"templates[{i}].match_hints.{k} must be a list"
                )
            match_hints[str(k)] = [str(x) for x in v]
        parsed.append(
            Template(
                id=tid, name=name, hint=hint, body=body,
                match_hints=match_hints, enabled=enabled,
            )
        )

    # Duplicate ids would make the selector ambiguous — reject early.
    seen: set[str] = set()
    for t in parsed:
        if t.id in seen:
            raise TemplateLibraryParseError(f"duplicate template id: {t.id!r}")
        seen.add(t.id)

    return TemplateLibrary(version=version, templates=tuple(parsed))


# ── Selector ────────────────────────────────────────────────────────────────


_SELECTOR_SYSTEM_PROMPT = (
    "You are a routing assistant. Given a list of script template options and "
    "a row's context (vertical, target country, article title and excerpt), "
    "pick the SINGLE best template id for this row.\n"
    "Hard rules:\n"
    "1. You MUST return an id that appears in the provided list — no inventions.\n"
    "2. If multiple templates fit, prefer the one whose hint most closely "
    "matches the article subject and tone.\n"
    "3. Return STRICT JSON with EXACTLY two keys: \"template_id\" and "
    "\"reason\" (one short sentence). Output nothing outside the JSON.\n"
)


def _format_user_message(
    *,
    library: TemplateLibrary,
    vertical: str,
    country: str,
    article_title: str,
    article_excerpt: str,
    safety: SafetyContext,
) -> str:
    enabled = library.enabled_templates()
    lines: list[str] = ["TEMPLATES:"]
    for t in enabled:
        hint = t.hint or "(no hint provided)"
        lines.append(f"- id: {t.id}\n  name: {t.name}\n  hint: {hint}")

    lines.append("")
    lines.append("ROW CONTEXT:")
    lines.append(f"  vertical: {vertical or '(unknown)'}")
    lines.append(f"  country: {country or '(unknown)'}")
    lines.append(f"  article_title: {article_title or '(unknown)'}")
    snippet = (article_excerpt or "").strip()[:500]
    lines.append(f"  article_excerpt: {snippet or '(empty)'}")

    if safety.matched:
        lines.append("")
        lines.append(
            "SAFETY NOTE: this row is flagged as sensitive apparel; "
            "prefer a template whose voice can stay product-focused."
        )
    return "\n".join(lines)


async def select_default_template(
    client: OpenAIClient,
    *,
    library: TemplateLibrary,
    vertical: str,
    country: str,
    article_title: str,
    article_excerpt: str,
    safety: SafetyContext = SAFE,
    model: str = MODEL_SCRIPT_GEN,
) -> Template | None:
    """Pick the best library template for the row.

    Returns ``None`` when:
      - the library has no enabled templates,
      - the OpenAI call fails for any reason (retries are inside the client),
      - the JSON parse fails,
      - the returned id is not in the library (hallucination guard).

    The caller is expected to treat ``None`` as "no template selected — fall
    back to your existing default behavior."
    """
    enabled = library.enabled_templates()
    if not enabled:
        _log.info("selector_empty_library")
        return None

    # Shortcut: only one enabled template — pick it without burning an API call.
    if len(enabled) == 1:
        _log.info(
            "selector_single_template",
            template_id=enabled[0].id,
        )
        return enabled[0]

    user_msg = _format_user_message(
        library=library,
        vertical=vertical,
        country=country,
        article_title=article_title,
        article_excerpt=article_excerpt,
        safety=safety,
    )

    try:
        result = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": _SELECTOR_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            max_tokens=120,
            temperature=0.0,
        )
    except Exception as e:    # selector failure must NEVER block the row
        _log.warning(
            "selector_call_failed",
            error=type(e).__name__,
            detail=str(e)[:200],
        )
        return None

    try:
        parsed: dict[str, Any] = json.loads(result.text)
    except json.JSONDecodeError as e:
        _log.warning(
            "selector_parse_failed", error=str(e), raw_preview=result.text[:200]
        )
        return None

    raw_id = str(parsed.get("template_id") or "").strip()
    if not raw_id:
        _log.warning(
            "selector_no_id", parsed_keys=list(parsed.keys()),
        )
        return None

    template = library.by_id(raw_id)
    if template is None:
        # Hallucinated or disabled — never trust raw model output back into
        # the prompt assembly path.
        _log.warning(
            "selector_invalid_id",
            returned_id=raw_id,
            valid_ids=[t.id for t in enabled],
        )
        return None

    reason = str(parsed.get("reason") or "").strip()
    _log.info(
        "selector_chose",
        template_id=template.id,
        reason=reason[:200],
        vertical=vertical[:40],
        country=country[:40],
        cost_usd=result.cost_usd,
    )
    return template
