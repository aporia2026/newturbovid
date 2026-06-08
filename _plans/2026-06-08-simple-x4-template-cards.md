# Simple x4 — per-video template + CTA selection

Date: 2026-06-08
Status: draft — pending Yoav approval
Owner: Yoav
Source: Yoav 2026-06-08 (sidebar screenshots + the two template mockups + the German cars example)
Builds on: `_plans/2026-06-02-aporia-bulk-video-tool.md` (master), `_plans/2026-06-07-overload-handling-and-template-defaults.md` (template_selector + admin library)

## Context

The `simple x4` tab (named by the operator; in code it routes to the `image_vo` pipeline via [Code.gs:106](apps_script/Code.gs#L106)) generates 4 videos per row from one Manual Image used as inspiration. Today each panel of the 4 is produced by kie.ai with the seed image's banner-style headline + CTA baked into the AI output ([image_prompt.py:82-87](src/bulkvid/pipeline/image_prompt.py#L82-L87)).

Yoav wants per-video control: for each of the 4 videos in a row, pick one of two visual card styles (blue/purple square, green gradient — mockups attached to source convo) and a CTA text. Blank choices keep today's behavior. Operators see a preview thumbnail of the picked template inline in the sheet.

## Goals

- **G1.** An operator can choose, per-video, one of N template styles + a CTA text from the sheet, without leaving the sheet. Blank = today's behavior, unchanged.
- **G2.** Templates 1 and 2 render pixel-perfectly per the supplied PNG mockups — no AI text drift, no font roulette, no off-brand CTAs.
- **G3.** The chosen card design adapts to any aspect ratio the row's `Change Size` column requests (9:16, 1:1, 16:9, etc.).
- **G4.** Operators see the preview thumbnail of their pick inline next to the row, so wrong picks are obvious before submit.
- **G5.** Observability (rule 14): every template render emits namespaced logs (`[template render]`, `[template overlay]`) with the values used, so a wrong color or CTA on a finished video is diagnosable from logs alone.
- **G6.** Defaults are configurable in the admin panel (rule 15): default CTA per template, master enable-switch, template library extensible without redeploy.

## Constraints

- **Scope: `simple x4` tab ONLY.** Other tabs unchanged. The image_vo column map split below isolates the change.
- **No new paid services.** Headline source uses the script-generation chain that already runs (no extra GPT call) — see §Cost.
- **Backwards compatible.** Rows with empty Template* cells run the existing pipeline and produce byte-identical output (within Rendi's nondeterminism). Old rows that already have `Ready Video N` URLs in cols J-M today must keep working after the column shift — see §Migration.
- **Single deploy unit.** Apps Script (Sheet UI) + Python backend (column maps, renderer, pipeline) deploy together. Out-of-order deploys break submission until both sides match — see §Rollout.
- **No `--no-verify` shortcuts** (rule 18). Tests gate the merge.
- **Rule 5 (no AI-template look).** The blue/purple and green-gradient designs ARE the user-provided mockups; we render them as specified, not invented. Default style is whatever kie produces today (also operator-supplied via prompt library).

## Requirements

In scope:
- 8 new columns (Template1, CTA1, Template2, CTA2, Template3, CTA3, Template4, CTA4) inserted between H (`Script Pattern`) and the old col I (`Open Comments`) on the `simple x4` tab. Per-video Template + CTA.
- Row 1 becomes a frozen header row: "Template Preview" label in col A, the Template-1 preview image (`=IMAGE(...)`) in one cell, the Template-2 preview image in the next. Row 2 holds the actual column headers. Row 3+ holds data. (Yoav 2026-06-08 — the user is laying this out in the sheet manually.)
- New Python column map `SIMPLE_X4_COLS` (parallel to `IMAGE_VO_COLS`, not a mutation of it) so other deployments using the legacy image_vo layout don't break.
- Per-video Template choice ∈ {empty, `1`, `2`} (extensible).
- Per-video CTA text (free-form, ≤80 chars).
- Pillow-based card renderer producing the title strip + CTA button overlay at the row's aspect ratio.
- Auto-populated `=IMAGE(...)` in the Preview cells via Apps Script `onEdit` trigger (lazy-user bar, rule 10).
- Settings registry entries for the two default CTAs and the master enable switch.
- Unit tests covering: column shift, renderer at all supported aspect ratios, per-template CTA fallback, empty-Template path (no overlay applied), and a regression test for the existing image_vo path that proves it still produces identical output when used on a non-simple-x4 tab.

Out of scope (separate plans if/when needed):
- Adding the feature to `simple`, `cartoon`, or `four_images_vo2` tabs.
- More than two new templates beyond the default. The registry shape is built to extend, but new template renderers are a follow-up.
- Per-video Headline override (always derived once per row from the script generation, used on all 4 videos).
- A template *designer* UI. Templates ship as code-defined renderers; mockup → code is a manual step per new template.
- Video-level editing tools (trim, fade, music swap) that aren't currently controllable per-video.

## Current state — what changes

| Surface | Today | After this plan |
|---|---|---|
| `simple x4` tab columns | H=Script Pattern, I=Open Comments, J-M=Ready Video 1-4 | H=Script Pattern, I-P=Template1..CTA4 (8 cols), Q=Open Comments, R-U=Ready Video 1-4; row 1 reserved as a frozen header that shows the two template preview images |
| `IMAGE_VO_COLS` (Python) | shared by simple x4 + any image_vo-shaped tab | unchanged; legacy callers untouched |
| New `SIMPLE_X4_COLS` (Python) | — | new map, only the `simple x4` tab uses it |
| Tab detection in `_detectTabType` | `simple x4` → returns `TAB_IMAGE_VO` | introduce `TAB_SIMPLE_X4` constant, returned for the `simple x4` name; `TAB_IMAGE_VO` reserved for legacy / header-detected tabs |
| `ImageVORow` dataclass | one `script_pattern` field | new `SimpleX4Row` dataclass with 4 (template, cta) pairs |
| `process_image_vo_row` | always produces 4 raw quadrants | when any Template* set, applies Pillow overlay to that quadrant before the upload-to-storage step |
| Headline source | baked into the kie collage prompt | unchanged — used for default. For Template 1/2, kie is prompted to suppress text, and the Pillow overlay draws the headline + CTA |
| Sidebar `chosen_template_id` | unrelated (script template library) | unchanged — these are different "templates"; see §Naming below |

## Naming — the existing "Template" collision

Reminder from convo: the existing `script_pattern` column already routes through a "template" library — the *script-side* selector ([template_selector.py](src/bulkvid/pipeline/template_selector.py)) writes `chosen_template_id` to row metadata. That feature is about *which script body* to write.

The new columns are also called `Template` (operator-facing) but mean *which visual card to render around the video*. To keep the code unambiguous:

- Operator-facing column header stays `Template1..4` per Yoav's labeling.
- Python field on the row dataclass: `card_template_1..4` (so it never collides with `script_pattern` / `chosen_template_id`).
- Log namespace: `[card render]`, `[card overlay]` — distinct from `[script selector]` and `[script render]`.

This is the only place in the plan where the UI name and the internal name diverge. Worth the small mismatch; the alternative (calling the column "Card Style") was offered and Yoav explicitly kept "Template".

---

## Design

### D.1 Sheet column layout (Apps Script + Python)

`simple x4` tab layout, 1-indexed. Yoav 2026-06-08: previews live in a frozen row 1 (NOT a per-row column), so the column insert is 8 — Template/CTA pairs only.

```
ROW 1 (frozen header — preview reference)
  A  "Template Preview"  label
  ... two of the columns aligned with Template1/Template2 hold
      =IMAGE("https://storage.googleapis.com/.../template_1.png") and
      =IMAGE("https://storage.googleapis.com/.../template_2.png")
      so the operator scrolls down through hundreds of rows with the
      two preview images always visible at the top.

ROW 2 (frozen — column headers)
  Country | Vertical | Article | Manual Image | Voice Over | ZapCap |
  Change Size | Script Pattern |
  Template 1 | CTA 1 | Template 2 | CTA 2 | Template 3 | CTA 3 |
  Template 4 | CTA 4 |
  Open Comments | Ready Video 1 | Ready Video 2 | Ready Video 3 |
  Ready Video 4

ROW 3+ (data) — column indexes:
 1  Country
 2  Vertical
 3  Article
 4  Manual Image
 5  Voice Over
 6  ZapCap
 7  Change Size               (aspect ratio)
 8  Script Pattern
 9  Template1                 NEW — dropdown: blank | "1" | "2"
10  CTA1                      NEW — free text, ≤80 chars
11  Template2                 NEW
12  CTA2                      NEW
13  Template3                 NEW
14  CTA3                      NEW
15  Template4                 NEW
16  CTA4                      NEW
17  Open Comments
18  Ready Video 1
19  Ready Video 2
20  Ready Video 3
21  Ready Video 4
```

Important: data starts at **row 3**, not row 2, because row 1 is the preview header and row 2 is the column-name header. Existing readers that assume `data[1:]` (skip one header row) need to skip TWO rows now. See §D.9 below.

Files to update:

- [src/bulkvid/adapters/sheets.py](src/bulkvid/adapters/sheets.py) — add `_SimpleX4Cols` dataclass and `SIMPLE_X4_COLS` constant; new `read_simple_x4_rows()` method; extend `batch_write_video_urls` to handle `TAB_SIMPLE_X4` for the Ready Video column lookup.
- [src/bulkvid/models/row.py](src/bulkvid/models/row.py) — add `SimpleX4Row` dataclass with 12 new fields wrapped as `cards: list[CardChoice]` of length 4.
- [src/bulkvid/orchestrator/queue.py](src/bulkvid/orchestrator/queue.py) — add `TAB_SIMPLE_X4 = "simple_x4"` constant; route payload deserialization.
- [apps_script/Code.gs](apps_script/Code.gs) — add `SIMPLE_X4_COLS` constant (mirror of Python map); update `_detectTabType` to return `TAB_SIMPLE_X4` for the `simple x4` name (keep falling through to `TAB_IMAGE_VO` for header-only detection); new `_readSimpleX4Row()`; add `onEdit` trigger that populates the Preview cell when a Template cell changes.

### D.2 Card-renderer architecture — the real fork

Three viable approaches, only the chosen one will be implemented:

**R1 — Pillow overlay (Recommended).**
Each Template ∈ {1, 2} is a Python class that takes `(background_image_bytes, headline, cta_text, width, height)` and returns composited PNG bytes. The default (empty Template) skips this step entirely. Pillow is already in the dependency tree and already used for the 2x2 quadrant split ([image_ops.py](src/bulkvid/image_ops.py)).

- Pros: pixel-perfect mockup match; deterministic across runs; full control over fonts/colors/gradients/CTA button pill; testable with image-diff assertions; no extra paid API call.
- Cons: card design lives in code, not in a config blob — adding template 3 is a small dev task (~30 lines + the design). Per-aspect-ratio testing matrix.

**R2 — Prompt-based templates (kie does it).**
Add a different collage prompt per template that asks kie.ai to produce a blue/purple-style or green-gradient-style ad. CTA text is interpolated into the prompt.

- Pros: minimal pipeline change; reuses the kie path.
- Cons: AI text rendering drifts (typos, font choices, color variance); pink CTA pill won't match the mockup pink; cannot guarantee the layout. Every regeneration of the same row produces a slightly different card. Fails goal G2.

**R3 — Hybrid.**
Use kie for the photo + Pillow for the title strip and CTA pill only. Background photo fills the main canvas; overlays draw the deterministic UI on top.

- Pros: photo realism from kie + deterministic UI from Pillow. Close to how the mockups look (the colored block in mockup 1 is the photo area; the pink CTA pill is the deterministic part).
- Cons: two-stage composite; need to extend the kie prompt to skip drawing text (otherwise text appears twice). Slight risk of overlap if the kie image already has bottom-strip content from the seed.

**Chosen: R1 for the initial 2 templates, with R3 as a follow-up if Yoav wants the photo-as-background design.**

Looking at the mockups again: the blue square IS solid blue, not a photo — it's a backdrop with the headline + CTA at the bottom. The green gradient is the same shape. So R1 is sufficient for the two supplied designs. If a future template wants a photo background, we add R3 then (the overlay code from R1 is reusable; only the background source changes).

### D.3 Where in the pipeline the overlay applies

In [row_processor_image_vo.py](src/bulkvid/orchestrator/row_processor_image_vo.py) — after Stage 7 (quadrant optimization), before Stage 7's upload-to-storage call:

```
Stage 6: PIL split into 4 quadrants                  (unchanged)
Stage 6b (NEW): apply per-quadrant card overlay      (only when card_template_N set)
Stage 7: upload optimized quadrants to storage       (unchanged path; takes the post-overlay bytes)
Stage 10: Rendi stills_to_video x 4                  (unchanged)
```

Rendi only ever sees a finished PNG; no template knowledge in the ffmpeg layer. ZapCap (Stage 12) still works because it operates on the final video, not the image.

### D.4 Headline source

Already settled: AI-generated from the article (current behavior). Today the headline is generated INSIDE the kie collage prompt, not surfaced as a Python string.

For the overlay path, we need the headline as a Python string. Two micro-options:

- **H1 (Recommended)**: extract the headline as a separate small GPT call in `script_side` (the script-generation coroutine that already runs concurrently with image_side). Return `(script, style, language, vo_url, headline)`. No new round-trip — reuse the same gpt-5.4-mini context.
- **H2**: derive the headline from the script's first sentence client-side (no extra GPT call). Cheaper but the script is voiceover-shaped (~16-18 words, neutral) and a headline wants different cadence; outputs will feel off.

**Chosen: H1.** Adds one ~50-token GPT call per row when at least one Template* is non-blank. Skipped entirely when all 4 are blank.

### D.5 CTA fallback

Settled: per-template default CTA. Two new settings in the registry:

- `card_template_1_default_cta` — defaults to `"DISCOVER MORE >>"`
- `card_template_2_default_cta` — defaults to `"See The Full Guide >>"`

The renderer uses the row's CTA cell if non-blank, otherwise the setting. If both are blank (e.g. setting cleared by the admin), the CTA pill is omitted entirely so the card never shows an empty button.

### D.6 Aspect ratio handling

Settled: single template definition, renderer scales. Each Card renderer takes `(width, height)` and computes:
- title strip height = `H * 0.18` (the bottom ~18% of the canvas for the colored strip — matches both mockups)
- CTA pill = positioned within the strip, width auto-fit to text + padding, vertically centered
- font size = scales with `H` (clamped to a min/max so the headline stays readable at extreme ratios)
- For mockup 1 (blue square): the top 82% is the solid blue background; for mockup 2 (green): the entire canvas is the gradient, with the title strip semi-transparent over the gradient

If the row's `Change Size` produces a ratio outside `VALID_RATIO_STRINGS` (see [rendi.py:114](src/bulkvid/adapters/rendi.py#L114)), fall back to 9:16 — same fallback the rest of the pipeline already uses, so the card matches the actual video aspect.

### D.7 Preview thumbnail mechanism

Yoav 2026-06-08: previews live in a frozen row 1, NOT in a per-row column. Two cells in row 1 (aligned with Template 1's and Template 2's columns) hold `=IMAGE(<url>)` formulas pointing to the corresponding preview PNG. The operator scrolls through hundreds of data rows with the two preview images always pinned to the top of the viewport.

Why this is right: the operator's question is "which template number gives me which look?" — it has one global answer, not per-row. Putting the previews in a frozen header costs zero per-row cells and matches how Google Sheets users naturally scan a long sheet (down through rows, look up to the header for reference).

Implementation:
- Two source PNGs from `apps_script/template_previews/template_1.png` and `template_2.png` uploaded once to GCS at `bulkvid/templates/template_1.png` and `bulkvid/templates/template_2.png`.
- A one-shot tool (`tools/upload_template_previews.py`) takes the user-provided PNGs and uploads them with the right MIME + cache headers. Run once per template-asset update.
- The migration menu item writes the `=IMAGE(<url>)` formulas into the row 1 cells where Yoav's manual setup placed the "1" and "2" labels (cols C and D — left-side of the sheet so they stay visible regardless of horizontal scroll position) and freezes rows 1 + 2 so they stay visible while scrolling.

### D.8 Validation

Apps Script side (immediate, before submit):
- `_validateSimpleX4(r)` — Template* values must be `""`, `"1"`, or `"2"`. CTA* length ≤ 80. If Template_n is set but the corresponding aspect ratio in `Change Size` is unrecognized, warn but don't refuse.
- Use Data Validation on the Template* columns (dropdown: blank, `1`, `2`) so operators can't type free text — lazy-user bar (rule 10).

Backend side (defense in depth):
- `read_simple_x4_rows` rejects any row whose Template_n is not in the allowed set; logs the bad value, skips the row with a clear error string (rule 13 — never trust the client).
- CTA texts longer than 80 chars are truncated with an ellipsis and a warning log; 80 chars is the visual upper bound of a one-line CTA pill at 9:16/1080w.

---

## UI/UX (rule 16)

- **Sheet headers** for the 12 new columns use the same casing/font as existing headers; Template + Preview + CTA columns for one video are visually grouped via background color (light blue for video 1, light green for 2, etc.) so operators scan video-by-video, not column-by-column.
- **Preview cell** shows the actual PNG inline at row height; clicking the cell opens the full-resolution image in a new tab (Google Sheets `=IMAGE` default behavior).
- **Dropdown on Template** restricts to `(blank) | 1 | 2`. No free typing.
- **CTA cell** has a tooltip / data-validation note: "Empty = template default. Max ~80 characters."
- **Sidebar** ([Sidebar.html](apps_script/Sidebar.html)) — for `simple x4` rows, the per-row breakdown shows each video's chosen template + CTA under "Row N" in the active card, so operators can confirm the right combo is being rendered without opening the sheet. Reuses the existing `row-template` caption style ([Sidebar.html:151-161](apps_script/Sidebar.html#L151-L161)).

## Security (rule 13)

- **Validation at boundaries** — Apps Script + Python both validate Template* and CTA* values. Python never trusts the values Apps Script sends.
- **CTA text rendered with Pillow as a typeset string**, NEVER concatenated into HTML/JS/shell. Pillow's `ImageDraw.text` treats the input as opaque text, no injection surface.
- **Preview image URLs are signed/public-read but read-only** — no operator can mutate the template asset by editing the sheet. Asset rotation = re-run `tools/upload_template_previews.py`.
- **No new outbound network surface.** All rendering happens in-process; no third-party design API.
- **Logs never include CTA text full-content** at INFO level — truncated to 40 chars + length, in case an operator pastes PII or a tracking URL. Full text only at DEBUG.

## Observability (rule 14)

Every step the new code introduces emits a namespaced log:

- `[card validate]` — Apps Script: which Template* / CTA* were read for the row.
- `[card overlay]` — Python: template id, headline length, cta text (truncated 40), aspect, output bytes size.
- `[card render]` — Python: rendering wall time, Pillow operation count, font load result.
- `[card preview]` — Apps Script `onEdit` trigger: which cell changed, which preview URL written, errors when the URL is unreachable.

Mirrors the shape established in rule 14 (`[ns step]`) and matches what `_log = get_logger("imageprompt")` already does. When a finished video shows the wrong CTA, the sequence above is enough to localize the bug to one of: validation (Apps Script sent wrong value), template lookup (wrong setting), or render (renderer bug).

## Settings audit (rule 15)

New entries in [runtime_settings.py](src/bulkvid/orchestrator/runtime_settings.py) `SETTINGS_REGISTRY`:

| Key | Default | Purpose |
|---|---|---|
| `card_template_1_default_cta` | `DISCOVER MORE >>` | Fallback CTA when CTA1/2/3/4 cell empty AND Template=1 |
| `card_template_2_default_cta` | `See The Full Guide >>` | Fallback CTA when cell empty AND Template=2 |
| `card_templates_enabled` | `true` | Master switch — flip to `false` to make every Template* cell be treated as blank (instant kill switch if the renderer misbehaves in production) |
| `card_preview_url_template_1` | `https://storage.googleapis.com/<bucket>/bulkvid/templates/template_1.png` | URL of the Template 1 preview PNG. Used by the migration helper to write the `=IMAGE(...)` formula into the row 1 frozen header cell. Swap to re-host = single setting change, no redeploy. |
| `card_preview_url_template_2` | `https://storage.googleapis.com/<bucket>/bulkvid/templates/template_2.png` | Same for Template 2. |

Why these specifically:
- Per-template default CTA — settled in convo.
- Master switch — if a render bug ships, no redeploy needed to recover; operators flip to `false` and rows render as if Template* were all blank.
- Preview URLs — externalized so swapping a preview PNG doesn't require a code change.

Not exposed (deliberately):
- Title strip color, font, layout — these define the template *identity*; per-row tweaks defeat the purpose. New visual styles = new template id, not a settings flag.

## Testing (rule 18)

New test files:

- `tests/unit/test_sheets_simple_x4.py` — round-trip a sample `simple x4` row through `read_simple_x4_rows`, verify the 12 new fields parse correctly, missing/blank values default sanely, malformed Template* values are rejected with a warning log.
- `tests/unit/test_card_renderer.py` — render Template 1 + Template 2 at every supported aspect ratio (10 ratios); assert output byte size > 1KB and < 2MB; assert specific pixel colors at the title-strip / CTA-pill locations (so a CSS-level regression is caught). Use Pillow's `getpixel` for the assertions.
- `tests/unit/test_row_processor_simple_x4.py` — integration test for the row processor with mocked clients; verify that an empty-Template row produces byte-equivalent output to the existing image_vo path; that a Template=1 row triggers the overlay step and the resulting image differs from raw; that the per-template default CTA fallback works.
- `tests/unit/test_apps_script_validation.py` — JS unit tests via the existing apps_script test harness (if present; otherwise extend the existing manual test in [tests/unit/test_runner.py](tests/unit/test_runner.py)) for the `_validateSimpleX4` function.

Regression coverage:
- Existing `test_row_processor_image_vo.py` must still pass unchanged (proves the legacy image_vo path is untouched).
- Bug-fix-as-test discipline (rule 18): if QA finds a CTA-truncation bug after merge, the fix lands with a test that fails on the old code and passes on the new.

Out-of-scope test types: end-to-end with real Rendi/kie/GCS (too slow for CI; manual smoke before deploy).

## Cost (rule 8)

- **Headline GPT call (H1)** — one extra gpt-5.4-mini call per row that has ≥1 non-blank Template*. ~50 input + ~30 output tokens. At current pricing (per [models.dev](https://models.dev/), to verify before merge): roughly $0.0001 per row. Negligible relative to the kie image generation (~$0.04/row) and Rendi (~$0.04/row).
- **No new paid services.** GCS uploads of preview PNGs are one-time, ~50KB each.
- **Pillow rendering** runs in-process on the existing PythonAnywhere worker; no CPU cost beyond what's already budgeted.

**Verification before merge**: pull live pricing from models.dev for gpt-5.4-mini and confirm the per-row cost estimate above. Per rule 1 (verify, don't guess) and rule 8 (real current pricing).

---

## Migration / rollout

The column shift breaks any sheet that has data in the old Open Comments column (col 9) and Ready Video columns (10-13) the moment the Apps Script + Python are deployed — unless the data also moves rightward by 8 columns. Additionally, every existing data row was at row N (1-indexed, header at row 1); after the migration data starts at row 3 (row 1 = preview, row 2 = headers), so the row indexing changes.

**Mitigation: a one-shot Apps Script migration menu item** (`Aporia Bulk Video → Migrate simple x4 columns`) that:
1. Verifies the active tab is `simple x4`.
2. Confirms with the operator (irreversible operation, rule on destructive actions). Yoav 2026-06-08: no active jobs at migration time — pre-check passes.
3. Inserts a new row 1 above the existing header row, pushing today's row 1 (column names) down to row 2 and all data rows down by one. Writes "Template Preview" label into A1.
4. Inserts 8 empty columns between col 8 and col 9.
5. Writes the new column-name cells into row 2 (Template 1, CTA 1, ..., CTA 4).
6. Writes the `=IMAGE(<url>)` formulas into the row-1 cells aligned with the Template 1 and Template 2 columns (cols I and K).
7. Adds Data Validation (dropdown blank | 1 | 2) to all Template* columns.
8. Freezes rows 1 and 2 so the preview + headers stay pinned during scroll.

Deploy order:
1. Land Python changes first (new column map, new row dataclass, new pipeline branch) **with the new behavior gated behind `card_templates_enabled = false`**. Old image_vo path stays the default. No user-visible change.
2. Land Apps Script changes (new column map, new readers, migration menu item).
3. Operator runs the migration on the `simple x4` tab. Templates remain blank; pipeline runs exactly as before because the gate is off.
4. Flip `card_templates_enabled = true` in the admin panel. New behavior live.

Rollback at any step is the master switch (`card_templates_enabled = false`) — no code redeploy needed.

---

## Open questions (all answered 2026-06-08 — plan approved to execute)

1. ~~Templates: only the two mockups attached, or more designs incoming?~~ **Answered: just the two for now.**
2. ~~"DEUTSCHLAND BIETEN" graffiti overlay~~ **Answered: those are ZapCap captions, not part of the image. No renderer work needed — ignore entirely.**
3. ~~Preview PNGs~~ **Answered: saved at `apps_script/template_previews/template_1.png` and `template_2.png`.**
4. ~~Migration timing~~ **Answered: no active `simple x4` jobs at migration time. Proceed straight through.**

## Next steps once approved

1. Confirm answers to the four open questions above.
2. Build the renderer + tests first (smallest unit, no pipeline integration, easy to iterate visually). Yoav reviews the output PNGs against the mockups before pipeline integration starts.
3. Land the column-map + row-dataclass changes behind a feature flag.
4. Land the pipeline integration.
5. Land the Apps Script side + migration menu.
6. Smoke test end-to-end on a test row.
7. Flip the master switch.
