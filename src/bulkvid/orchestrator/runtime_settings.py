"""Registered runtime-editable settings.

This module defines the canonical list of settings the admin panel can edit
and their default values. Add new entries here when surfacing new knobs.

Each entry has:
  - ``key``  — stable identifier used in the SQLite store
  - ``label`` — short human label for the admin form
  - ``default`` — built-in default (overridden by the admin's SQLite value)
  - ``multiline`` — render as <textarea> in the form when True

History:
  - 2026-06-04: split the single ``script_system_prompt`` into one prompt per
    tab (Simple, Simple x4, Cartoon) and added a shared sensitive-apparel
    safety rule + vertical keyword list. See
    ``_plans/2026-06-04-sensitive-apparel-safeguard-and-per-tab-prompts.md``.
    The legacy key is preserved for migration; see
    ``SettingsStore.migrate_legacy_keys``.
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
- Target length: about 16-18 words (roughly 10-15 seconds when read aloud). Hard maximum: 20 words.
- Begin with a concise and interesting hook within the first 5-8 words.
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


# ── Cartoon planner prompt (extracted from cartoon_prompt._system_prompt) ────

# Placeholders the cartoon planner substitutes per row:
#   {language}     — detected article language (e.g. "en", "he")
#   {num_ideas}    — how many independent video ideas to generate
#   {num_shots}    — shots per idea
#   {target_words} — target word count for each voiceover line
#   {min_words}    — minimum acceptable word count
#   {max_words}    — maximum acceptable word count
CARTOON_PLANNER_PROMPT_DEFAULT = """You are a creative director making SHORT animated cartoon social videos from a news article. You plan the visuals and a tight voiceover.

Produce exactly {num_ideas} INDEPENDENT video ideas. Each idea is a separate ~6-7 second video told in exactly {num_shots} shots.

For EACH idea return:
- voiceover: ONE short spoken line in {language}, about {target_words} words ({min_words}-{max_words}), natural and engaging, readable in ~6-7 seconds. MUST be a COMPLETE THOUGHT that ends in a period, question mark, or exclamation mark. NEVER end on a conjunction (and, but, or, so, because, with, that, which, as) or a preposition — finish the sentence. The final word should feel CONCLUSIVE — a strong noun or an action verb that lands the thought. AVOID ending on a bare adjective ("independent", "smart", "ready", "different") or an abstract noun that begs a follow-up — the line must feel finished on first listen, not like it's about to continue.
- style_direction: a short delivery hint for the voice actor.
- shots: an array of exactly {num_shots} shots, each with:
    * scene: a vivid description of ONE cartoon scene (subject, setting, framing). Vertical composition.
    * motion: how that scene should gently animate (small, natural movements and subtle camera moves).

HARD RULES:
1. Use GENERIC, SYMBOLIC characters and objects only. NEVER depict a real, named, or recognizable public figure. NEVER name a real brand or manufacturer (e.g. say 'a compact car', NOT 'a Volkswagen'). Describe all vehicles, products, and signage as plain and unbranded — no logos, badges, or readable license plates.
2. Within one idea, keep ONE recurring main character and describe them IDENTICALLY across the shots (same age, hair, clothing) so the shots feel like one continuous scene.
3. NO legible on-screen text: keep any screens, signs, phones, or papers abstract, blurred, or out of focus. Do not ask for words or numbers.
4. Keep it tasteful and brand-safe.

Return STRICT JSON only, shaped exactly like:
{{"ideas": [{{"voiceover": "...", "style_direction": "...", "shots": [{{"scene": "...", "motion": "..."}}]}}]}}"""


# ── Sensitive-apparel safeguard (Evgeny 2026-06-04) ──────────────────────────

SENSITIVE_APPAREL_RULES_DEFAULT = """SENSITIVE APPAREL: STRICT VISUAL RULES
This row's product is intimate apparel, swimwear, body shapers, or similar sensitive clothing. These rules OVERRIDE any conflicting guidance above and are non-negotiable for this row.

VISUALS - product only, no humans:
- Show ONLY the product on a clean, neutral background (white, beige, or soft pastel). Folded on a plain surface, on a hanger, or as a flat-lay are all fine.
- NO humans, NO mannequins or dress forms, NO body parts (face, torso, hands, legs, feet), NO silhouettes or shadows of people, NO implied wearer.
- NO suggestive posing or framing.

VOICEOVER - product attributes only:
- Talk about fabric, fit, comfort, design, color, care, materials, technology.
- Do NOT describe how the product looks on a body, do NOT reference body parts or shape, do NOT use suggestive or sensual phrasing."""


SENSITIVE_APPAREL_KEYWORDS_DEFAULT = (
    "underwear, lingerie, bra, bras, panties, panty, intimate apparel, "
    "intimates, swimwear, swimsuit, bikini, body shaper, shapewear, thong, "
    "thongs, briefs, boxers, sleepwear, nightwear, hosiery, stockings"
)


# ── Setting registry ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    default: str
    multiline: bool = False
    description: str = ""


# Legacy single-prompt key. Kept as a constant for the migration helper in
# ``settings_store.py``; intentionally NOT in the registry so it no longer
# appears in the admin panel.
SETTING_SCRIPT_SYSTEM_PROMPT = "script_system_prompt"

SETTING_SIMPLE_SCRIPT_PROMPT = "simple_script_prompt"
SETTING_SIMPLE_X4_SCRIPT_PROMPT = "simple_x4_script_prompt"
SETTING_CARTOON_PLANNER_PROMPT = "cartoon_planner_prompt"
SETTING_SENSITIVE_APPAREL_RULES = "sensitive_apparel_rules"
SETTING_SENSITIVE_APPAREL_KEYWORDS = "sensitive_apparel_keywords"


SETTINGS_REGISTRY: tuple[SettingDef, ...] = (
    SettingDef(
        key=SETTING_SIMPLE_SCRIPT_PROMPT,
        label="Simple: script prompt",
        default=SCRIPT_SYSTEM_PROMPT_DEFAULT,
        multiline=True,
        description=(
            "System prompt sent to gpt-5.4-mini when generating the voiceover "
            "script for rows on the Simple and 4Images-VO2 tabs. Use "
            "{language}, {country}, {vertical}, and {script_pattern} as "
            "placeholders — they're substituted per row."
        ),
    ),
    SettingDef(
        key=SETTING_SIMPLE_X4_SCRIPT_PROMPT,
        label="Simple x4: script prompt",
        default=SCRIPT_SYSTEM_PROMPT_DEFAULT,
        multiline=True,
        description=(
            "System prompt sent to gpt-5.4-mini when generating the voiceover "
            "script for rows on the Simple x4 (Image-VO) tab. Same "
            "placeholders as the Simple prompt."
        ),
    ),
    SettingDef(
        key=SETTING_CARTOON_PLANNER_PROMPT,
        label="Cartoon: planner prompt",
        default=CARTOON_PLANNER_PROMPT_DEFAULT,
        multiline=True,
        description=(
            "System prompt sent to gpt-5.4-mini when planning Cartoon-tab "
            "videos (voiceover + scene descriptions). Use {language}, "
            "{num_ideas}, {num_shots}, {target_words}, {min_words}, and "
            "{max_words} as placeholders."
        ),
    ),
    SettingDef(
        key=SETTING_SENSITIVE_APPAREL_RULES,
        label="Sensitive apparel: safety rules",
        default=SENSITIVE_APPAREL_RULES_DEFAULT,
        multiline=True,
        description=(
            "Appended to the active prompt(s) of any row whose Vertical "
            "column matches one of the sensitive-apparel keywords below. "
            "Applies to all four tabs (Simple, Simple x4, Cartoon, "
            "4Images-VO2)."
        ),
    ),
    SettingDef(
        key=SETTING_SENSITIVE_APPAREL_KEYWORDS,
        label="Sensitive apparel: vertical keywords",
        default=SENSITIVE_APPAREL_KEYWORDS_DEFAULT,
        multiline=False,
        description=(
            "Comma-, newline-, or semicolon-separated list. Match is "
            "lowercase substring against the row's Vertical column. Add new "
            "terms here to widen the safeguard."
        ),
    ),
)


def registry_defaults() -> dict[str, str]:
    """Return ``{key: default}`` for every registered setting."""
    return {s.key: s.default for s in SETTINGS_REGISTRY}


def lookup(key: str) -> SettingDef | None:
    return next((s for s in SETTINGS_REGISTRY if s.key == key), None)
