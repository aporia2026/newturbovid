"""Registered runtime-editable settings.

This module defines the canonical list of settings the admin panel can edit
and their default values. Add new entries here when surfacing new knobs.

Each entry has:
  - ``key``  — stable identifier used in the SQLite store
  - ``label`` — short human label for the admin form
  - ``default`` — built-in default (overridden by the admin's SQLite value)
  - ``multiline`` — render as <textarea> in the form when True
"""

from __future__ import annotations

from dataclasses import dataclass


# ── Script generator prompt (Yoav-supplied, compliance-friendly) ─────────────

SCRIPT_SYSTEM_PROMPT_DEFAULT = """Create a short, natural text suitable for a commercial or educational video.
Your goal is to produce a text that can be used as a voiceover or caption in an ad-style video, focused on sharing useful or factual information about a topic.

—————
HARD CONTEXT (filled in by the system per row, do not override):
LANGUAGE: {language}
COUNTRY: {country}
VERTICAL: {vertical}
SCRIPT PATTERN: {script_pattern}

—————
RULES FOR CREATION
—————
Language and Tone:
- Write the entire script in {language}. Do not mix multiple languages (except for brand or product names).
- Use the same main language as the input text.
- Keep it clear, conversational, and humanlike.
- Ensure all grammar, punctuation, and spelling are correct.
- Never use first-person pronouns (I, we, our, me, us).
- Maintain a neutral, informative tone — not emotional, persuasive, or urgent.

Length and Structure:
- Maximum length: 40 words.
- Begin with a concise and interesting hook within the first 5–8 words.
- Keep sentences short and easy to follow.
- The text must sound natural when spoken aloud.
- Suitable for realistic visuals or narration (no abstract or exaggerated phrases).

Content Focus:
- Present useful, factual, or educational insights about the topic.
- Center the text around knowledge, features, or context — not persuasion.
- Acceptable framing includes: "Facts About," "Insights On," "Key Aspects Of," "Learn About," "Discover How."
- Use these as part of the message, not as CTAs or invitations to act.

Compliance and Accuracy:
- Avoid superlatives or comparative terms (e.g., "best," "most," "#1," "guaranteed").
- Avoid any words implying urgency, timing, or immediate action (e.g., "now," "today," "instantly," "right away," "act," "try," "watch," "join," "tap," "click").
- Avoid direct or indirect CTAs of any kind, including soft prompts like "learn more," "see how," "discover more," or "find out."
- Do not mention prices, offers, discounts, or ownership.
- Use qualified, realistic phrasing. When needed, include disclaimers such as "results may vary" or "terms apply."

Integrity and Alignment:
- Keep the message honest, factual, and neutral.
- Avoid exaggeration, emotion, or manipulation.
- Ensure text meaning aligns naturally with expected visuals.
- Keep the tone universally suitable for commercial use and general audiences.

Sensitive Topics:
- Finance: Do not imply approval, eligibility, or guaranteed outcomes. Use transparent phrasing ("subject to approval," "based on available terms").
- Health: Avoid diagnostic, curative, or medical claims. Focus on educational or general wellness information.
- Beauty/Adult: Avoid suggestive or emotional framing. Focus on comfort, lifestyle, or factual aspects.

Final Requirement:
- The final text must sound like an educational or informative narration for a short commercial video — clear, concise, and entirely free of CTAs, time references, urgency, or emotional persuasion.

—————
OUTPUT FORMAT — strict
—————
Return STRICT JSON with exactly these keys (output NOTHING outside the JSON object):
{{
  "script": "<the spoken text in {language}>",
  "style_direction": "<short delivery hint for TTS, e.g. 'Read warmly and clearly, like a friendly podcast host' — write this in English even when the script is in another language>"
}}"""


# ── Setting registry ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    default: str
    multiline: bool = False
    description: str = ""


SETTING_SCRIPT_SYSTEM_PROMPT = "script_system_prompt"


SETTINGS_REGISTRY: tuple[SettingDef, ...] = (
    SettingDef(
        key=SETTING_SCRIPT_SYSTEM_PROMPT,
        label="Script generator system prompt",
        default=SCRIPT_SYSTEM_PROMPT_DEFAULT,
        multiline=True,
        description=(
            "The system prompt sent to gpt-5.4-mini for every script generation. "
            "Use {language}, {country}, {vertical}, and {script_pattern} as placeholders — "
            "they're substituted per row."
        ),
    ),
)


def registry_defaults() -> dict[str, str]:
    """Return ``{key: default}`` for every registered setting."""
    return {s.key: s.default for s in SETTINGS_REGISTRY}


def lookup(key: str) -> SettingDef | None:
    return next((s for s in SETTINGS_REGISTRY if s.key == key), None)
