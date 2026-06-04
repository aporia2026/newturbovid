# Sensitive-apparel safeguard + per-tab editable prompts

**Date:** 2026-06-04
**Author:** Yoav + Claude
**Status:** Approved — ready to execute
**Trigger:** Evgeny Alexeev (bulk team) asked us to protect generated videos so
that intimate-apparel rows (bras, panties, lingerie, swimwear, body shapers…)
show ONLY the product, with no humans. Yoav extended the scope to all video
modes and asked for per-tab admin-editable prompts at the same time.

---

## 1. Goals

1. **Safety:** When a row's Vertical column matches a "sensitive apparel"
   keyword (admin-editable list), every video produced by that row must depict
   the product on a neutral background — no humans, mannequins, body parts,
   silhouettes, or anything human-shaped. The voiceover for that row focuses on
   product attributes only and avoids body-language phrasing.
2. **Admin control:** Each of the three user-facing tabs (Simple, Simple x4,
   Cartoon) has its OWN system prompt the admin can edit at
   `/admin/settings/...`. The safety rules and the keyword list are also
   admin-editable so we can tune without redeploying.

## 2. Constraints

- No new per-row LLM cost: detection is exact substring match against an
  admin-editable keyword list — no classifier call.
- Backward-compat: the deployed admin's existing `script_system_prompt`
  customization must not be lost. We migrate it into BOTH new tab prompts on
  first launch.
- The four row-processor public APIs (`process_simple_row`,
  `process_4images_vo2_row`, `process_image_vo_row`, `process_cartoon_row`)
  keep their signatures. The settings store is already threaded through; we
  reuse it.
- 4Images-VO2 is in scope as "Simple-like" (user supplies images, we still
  influence the script). It shares the Simple script prompt; the team didn't
  ask for a separate prompt and adding one would be extra surface for no win.

## 3. Detection

`bulkvid/pipeline/safety.py` (new):

```python
def parse_keywords(blob: str) -> tuple[str, ...]: ...
def is_sensitive_apparel(vertical: str, keywords: Iterable[str]) -> tuple[bool, str | None]:
    """Lowercase substring match; returns (matched, first matched keyword)."""
```

Default keyword list (admin-editable):
`underwear, lingerie, bra, bras, panties, panty, intimate apparel, intimates,
swimwear, swimsuit, bikini, body shaper, shapewear, thong, thongs, briefs,
boxers, sleepwear, nightwear, hosiery, stockings`.

The match is **substring on the Vertical column only** (case-insensitive). If
the Vertical column is empty the match returns `False`.

Why Vertical and not article body: the bulk team types the vertical
explicitly per row — it's the field they already use to steer the system, so
making it the trigger gives them a way to override (e.g. set the vertical to
"apparel - outerwear" instead of "underwear" to skip the safeguard).

## 4. Admin settings (final list)

We replace today's single `script_system_prompt` with five settings:

| Key                              | Label                                       | Used by                              |
|----------------------------------|---------------------------------------------|--------------------------------------|
| `simple_script_prompt`           | Simple — script prompt                      | Simple + 4Images-VO2                 |
| `simple_x4_script_prompt`        | Simple x4 — script prompt                   | Image-VO                             |
| `cartoon_planner_prompt`         | Cartoon — planner prompt                    | Cartoon                              |
| `sensitive_apparel_rules`        | Sensitive apparel — safety rules            | All four, when detection triggers    |
| `sensitive_apparel_keywords`     | Sensitive apparel — vertical keywords       | Detector                             |

The Simple x4 collage **image** prompt (`_COLLAGE_SYSTEM` /
`_collage_user_message`) is NOT exposed as a separate setting — it's a tightly
templated structural prompt the team shouldn't be editing in a textarea. The
safety block is still applied to it programmatically when detection triggers.

### Migration

On first SettingsStore load after deploy, if a row exists for the old
`script_system_prompt` key, copy its value into both `simple_script_prompt`
and `simple_x4_script_prompt` (skipping any that already exist), then leave
the old row in place (it's harmless and lets us roll back). One audit-log line
per copy.

## 5. Safety-rules default text (English)

```
SENSITIVE APPAREL — STRICT VISUAL RULES
This row's product is intimate apparel, swimwear, body shapers, or similar
sensitive clothing. Override any conflicting guidance above:

VISUALS — product only, no humans:
- Show ONLY the product on a clean, neutral background (white, beige, or soft
  pastel). Folded on a plain surface, on a hanger, or as flat-lay are all fine.
- NO humans, NO mannequins or dress forms, NO body parts (face, torso, hands,
  legs, feet), NO silhouettes or shadows of people, NO implied wearer.
- NO suggestive posing or framing.

VOICEOVER — product attributes only:
- Talk about fabric, fit, comfort, design, color, care, materials, technology.
- Do NOT describe how the product looks on a body, do NOT reference body parts
  or shape, do NOT use suggestive or sensual phrasing.

These rules are non-negotiable for this row.
```

This block is appended (with a clear `---` separator) to the active prompt at
runtime when detection triggers. The admin can edit the block freely in the
panel.

## 6. Application points

For each mode, when detection triggers we append the safety block to the
specified prompt(s):

| Mode               | Where the block lands                                     |
|--------------------|-----------------------------------------------------------|
| Simple             | script-gen system prompt                                  |
| 4Images-VO2        | script-gen system prompt                                  |
| Simple x4 (Image-VO) | script-gen system prompt **+** collage user-message     |
| Cartoon            | cartoon planner system prompt                             |

For Image-VO the collage user message has a "TOP-LEFT/TOP-RIGHT cell photo:
[new article-relevant scene]" structure — we append the safety block as a
trailing override section. The model already honors "override above" cues
elsewhere in that prompt.

## 7. Code changes (file map)

- **NEW** `src/bulkvid/pipeline/safety.py` — keyword parser + matcher + a tiny
  `SafetyContext` dataclass carrying `(matched: bool, matched_keyword: str | None)`.
- `src/bulkvid/orchestrator/runtime_settings.py`:
  - Add 4 new keys + defaults; keep `SETTING_SCRIPT_SYSTEM_PROMPT` constant
    available (but no longer in the registry) so migration can read the old
    value. Add `CARTOON_PLANNER_PROMPT_DEFAULT` constant (extracted from the
    current `cartoon_prompt._system_prompt`).
  - Add `DEFAULT_SENSITIVE_APPAREL_KEYWORDS` and `SAFETY_BLOCK_DEFAULT`.
- `src/bulkvid/orchestrator/settings_store.py`:
  - Add a `migrate_legacy_keys()` method called once from `main.py` startup.
- `src/bulkvid/pipeline/script_gen.py`:
  - `generate_script(...)` gains a `prompt_setting_key: str` argument
    (default = `SETTING_SIMPLE_SCRIPT_PROMPT`) and a `safety: SafetyContext | None`
    argument. When `safety.matched`, append the safety block (read from the
    store, key `sensitive_apparel_rules`) to the system prompt.
- `src/bulkvid/pipeline/cartoon_prompt.py`:
  - Extract the inline `_system_prompt(...)` body to a default constant in
    `runtime_settings.py` (`CARTOON_PLANNER_PROMPT_DEFAULT`); the function
    now formats whatever template the settings store returns (with
    `{language}`, `{num_ideas}`, `{num_shots}`, `{target_words}`,
    `{min_words}`, `{max_words}` placeholders) and appends the safety block
    if `safety.matched`.
  - `generate_cartoon_plan(...)` gains `settings_store` + `safety` args.
- `src/bulkvid/pipeline/image_prompt.py`:
  - `build_collage_prompt(...)` gains a `safety: SafetyContext | None` arg.
    When matched, appends the safety block to the user message before the
    LLM call.
- Each row processor (`row_processor_simple.py`,
  `row_processor_4images.py`, `row_processor_image_vo.py`,
  `row_processor_cartoon.py`):
  - After parsing the row, run `safety = detect(row.vertical, keywords)`.
  - Pass `safety` and the right `prompt_setting_key` down to
    `generate_script` / `build_collage_prompt` / `generate_cartoon_plan`.
  - Log `[safety detect]` with the matched keyword (or none) per row.

## 8. Observability

- `[safety detect] row=N matched=True/False vertical='...' keyword='...'`
  emitted once per row at the start of processing.
- `[safety applied] mode=image_vo step=collage_prompt matched=True` at each
  append site so we can grep "which prompts got safety this run".
- Settings-store cache TTL stays at 30s — admin edits to the safety rules or
  keywords take effect within a half-minute on the live worker.

## 9. Tests (tests/test_safety_*.py)

- `test_safety_detection.py` — exact match, partial substring,
  case-insensitive, empty vertical, no match, multi-word vertical that
  contains one keyword, keyword list with whitespace and trailing commas
  (parser robustness).
- `test_safety_prompt_assembly.py` — for each mode, given a sensitive row
  versus a non-sensitive row, assert the safety block is / is not present in
  the prompt that's sent to the LLM. Mocks the OpenAI client so no network.
- `test_settings_migration.py` — settings store seeded with the legacy
  `script_system_prompt` value migrates to both new keys; second startup is a
  no-op; an already-customized new key is not overwritten.

We will not delete existing tests; we update the few that import
`SCRIPT_SYSTEM_PROMPT_DEFAULT` to import it from `runtime_settings` via the
new constant name if any drift.

## 10. Security & safety

- Editorial / brand risk: This whole feature *is* the safety mitigation
  Evgeny asked for. Adding the safeguard tightens, not loosens, our
  editorial posture.
- Input handling: vertical text from the sheet is treated as opaque user
  input — `.lower()` + substring scan only, no regex parsing of user input,
  no shell, no SQL.
- Settings-write authorization: unchanged — admin panel is HTTP-Basic only,
  same as today.
- Logging: no PII in the safety logs (vertical + keyword strings only — both
  are admin-supplied content, not user data).

## 11. Lazy-user lens (rule 10)

- Admin panel: 5 settings instead of 1. Labels are explicit ("Simple — script
  prompt", "Sensitive apparel — vertical keywords"). The keyword list is a
  single-line input with a comma-separated value the admin can scan at a
  glance. The safety block is a textarea pre-filled with sensible defaults.
- For the bulk team: zero new UI. They keep using the same sheet columns.
  The safeguard kicks in automatically based on what they already type into
  Vertical. If they want to *disable* the safeguard for a specific row,
  they type a vertical that doesn't contain a sensitive keyword. That's the
  bypass.

## 12. Alternatives considered

1. **LLM-based detection** (keyword + a yes/no model call when uncertain).
   Rejected: adds ~30ms + a small token cost per row, plus a new failure
   surface, for marginal benefit over a tunable keyword list.
2. **One mega-prompt setting per tab including the safety rules inline.**
   Rejected: three drift copies of the same safety rules — exactly the bug
   pattern that the user's CLAUDE.md rule 2 ("extremely ordered and
   organized") tells us to avoid.
3. **Block sensitive rows entirely instead of rewriting the prompt.**
   Rejected: the team wants the videos shipped, just safely. Blocking would
   make TurboVid feel broken for a category they actively run.

## 13. Out of scope

- Per-vertical custom safety blocks (e.g. different rules for "swimwear" vs
  "lingerie"). One block covers the whole sensitive bucket for now.
- Image-content moderation on the generated frames (post-hoc check). The
  prompt-side guard is the cheap first line; we can add a post-gen check
  later if a leak slips through.
- Exposing the Simple x4 collage *image* prompt as an admin setting.

## 14. Acceptance

- Admin panel `/admin/settings` shows 5 rows.
- Setting a row's Vertical to `"Lingerie boutique"` triggers the safety
  block on all four modes (verified by mocked-LLM unit tests asserting the
  prompt content).
- Setting the Vertical to `"Smart home gadgets"` produces prompts without
  the safety block.
- `pytest -q` passes with at least the same number of tests as before (395),
  plus the new safety tests.
- Editing the safety block in the admin panel changes the prompt within the
  cache TTL (30s) on the live worker.
