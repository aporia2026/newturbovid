# 1-click-image-vid tab

Date: 2026-09-10
Status: APPROVED — implementing on `origin/main`
Author: Claude + Yoav
Branch / worktree: `one-click-image-vid` (git worktree off `origin/main`)

## Goal

A new `1-click-image-vid` Sheet tab. One row, one run: take the operator's single
source image and, in one pass, generate **4 related story images** (a narrative
arc: wonder → discovery → experiment → success), sequence them into **one
still-image video** with a voiceover, and burn in **ZapCap captions**. Output:
exactly ONE finished mp4 in the row's `Ready Video` cell.

Reference outputs Evgeny gave (the target look — static stills, captioned):
- `REPOSSESSEDCARS-MX-ES-NANOBANANA-9_16-STILLIMAGE-1-ZAPCAP-...mp4`
- `REPOSSESSEDCARS-BR-PT-NANOBANANA-9_16-STILLIMAGE-3-ZAPCAP-...mp4`

Confirmed with Yoav (2026-09-10):
- Image generation = **2×2 collage split** (one `nano-banana-2` generation from
  the source image, split into 4 panels). Cheapest, style-consistent, reuses the
  proven `image_vo` front-half. NOT 4 separate generations.
- Column layout = the recommended single-image / single-video layout below.

## Column layout (paste this header row into the tab)

```
A Country | B Vertical | C Article | D Manual Image | E Voice Over | F ZapCap |
G Change Size | H Script Pattern | I CTA | J CTA Text | K Open Comments |
L Ready Video
```

Identical semantics to the existing single-image tabs (`simple` / `simple-motion`):
`Change Size` blank → the source image's native pixel dimensions; `Voice Over`
blank → Yes; `ZapCap` blank → No; `CTA` handled exactly as `simple-motion` does
(spoken call-to-action folded into the script); `Open Comments` supports the
`use this script:` verbatim override, same as every other tab.

## Architecture — reuse, don't fork (rule 20)

Front-half (source image → 4 quadrant URLs) is the **exact `image_vo` chain**,
reused function-for-function so the image logic stays single-sourced:
`resolve_aspect_ratio` → article fetch + source pre-upload → `describe_source_image`
→ `build_collage_prompt` → `edit_with_fallback` (nano-banana-2 primary) →
`recraft_crisp_upscale` (soft-fallback to raw) → `split_collage_2x2` → optimise +
upload 4 quadrants. Runs concurrently with the script/TTS side, same as `image_vo`.

The only new prompt piece: a **story-mode collage prompt**. `image_vo`'s
`skip_text=True` path yields 4 *varied* clean photos but not a *narrative arc*.
Add `_collage_user_message_story(...)` in `pipeline/image_prompt.py` and a
`story: bool = False` flag on `build_collage_prompt` that routes to it. Story
prompt = strict 2×2 grid, NO text, a single recurring subject/scene, 4 sequential
beats (curiosity → discovery → trying it → happy result), article-grounded,
no real brands. Keeps all collage-prompt logic in one file (SSOT).

Back-half (4 stills → ONE captioned video) reuses the cartoon timing helpers so
the "size to VO, no dead air" math is single-sourced — import `_even_clips`,
`MIN_VIDEO_SECONDS`? (define locally, see Tunables), `VO_TAIL_SECONDS`,
`SILENT_SHOT_SECONDS` from `orchestrator.pinned_cartoon`:

- VO path: `raw = tts.duration_seconds`; `atempo = 1.0` (natural still-image pace);
  `total = max(OCI_MIN_VIDEO_SECONDS, raw + VO_TAIL_SECONDS)`;
  `per_clip = _even_clips(total, 4)`. Render each quadrant to a silent clip
  (`rendi.image_to_silent_video`, `seconds = ceil(max(per_clip)) + 1`), then
  `rendi.concat_clips_with_audio(clips, vo_url, per_clip, total_video_seconds=total,
  atempo=1.0)` → ONE video. Then ONE `zapcap.caption_video(video_duration_seconds=total)`.
- No-VO path: `total = 4 * SILENT_SHOT_SECONDS`; concat with `audio_url=None`.
  ZapCap is skipped when there is no VO (nothing to transcribe) — logged, row still
  ships the silent stitch.

Slot output is a single-element `video_urls=[final_url]`.

New file: `orchestrator/row_processor_one_click_image_vid.py` — a trimmed sibling
of `row_processor_image_vo.py` (same failure-status mapping, `_Costs`, `_ok`/`_fail`).
`process_image_vo_row` and every other processor are untouched.

## Tunables (this tab only)

- `OCI_NUM_IMAGES = 4` (the story arc length; matches the 2×2 split).
- `OCI_MIN_VIDEO_SECONDS = 8.0` (length floor; VO otherwise wins, no upper cap).
- `OCI_ATEMPO = 1.0` (natural pace — a slideshow, not a rushed read).
- Row timeout: `900s` (single-video, image-gen + TTS + 5 Rendi calls + ZapCap;
  lighter than the N-video 1800s tabs). Registered as a settings override.

## Wiring checklist (same 8-file footprint as fast-and-furious)

- `models/row.py` — new `OneClickImageVidRow(_MarketRow)` (mirrors `ImageVORow` +
  `cta` / `cta_text`).
- `routes/jobs.py` — `OneClickImageVidRowIn`, `SubmitJobIn.rows_one_click_image_vid`,
  `_build_one_click_image_vid_row`, submit dispatch branch.
- `orchestrator/queue.py` — `TAB_ONE_CLICK_IMAGE_VID`, model import,
  `payload_to_row` branch.
- `orchestrator/runner.py` — import processor + model, tab const, default timeout,
  timeout-setting key, `_dispatch_row` `isinstance` branch.
- `orchestrator/runtime_settings.py` — `SETTING_ROW_TIMEOUT_ONE_CLICK_IMAGE_VID`
  (+ definition). No custom planner prompt setting needed (story prompt is code).
- `adapters/sheets.py` — tab import, `_OneClickImageVidCols`, header-rows entry,
  `read_processed_row_nums` branch, `batch_write_video_urls` branch (writes ONE
  video URL to col L).
- `apps_script/Code.gs` — `TAB_ONE_CLICK_IMAGE_VID`, add to `SEEDANCE_VIDEO_TABS`
  (size dropdowns + open-comments tips), `_COLS`, `_detectTabType` branch,
  `_readOneClickImageVidRow`, `_validateOneClickImageVid`, and the four
  `generateAllUnprocessed` / `_submitJobForRowNums` ternary branches (cols,
  reader, validator, video-col, payload key `rows_one_click_image_vid`).
- New `pipeline/image_prompt.py` story prompt (above).

## Security & compliance (rule 13)

Paid native generation over sensitive verticals (finance / property / health):
the story collage prompt keeps the same HARD fences as the other tabs — no real
brands/logos/plates/legible text, generic people, realistic photos, article-
faithful (no invented claims). The sensitive-apparel safety block is threaded
through `build_collage_prompt` unchanged. The `use this script:` verbatim override
is operator-approved copy and is logged/flagged (`script_used_override`,
`script_override_oversize`) exactly as elsewhere. Source-image URL flows through
the existing `url_guard` / `download_image` path (SSRF guard, size caps). No new
secrets, no new external surface — same KIE / Rendi / ZapCap / Gemini adapters.

## Observability (rule 14)

Namespaced `[row ...]` structured logs mirror `image_vo`: `row_start`,
`describe_ok`, `collage_prompt_ok`, image-gen, `upscale_failed_kept_raw`,
per-quadrant upload, plus new tab-specific lines — `oci_timing` (raw VO, total,
per_clip, atempo), `oci_concat_ok`, `oci_zapcap_skipped_no_vo`, `row_done` /
`row_failed` with the full `cost_breakdown`. Every failure maps to a precise
`STATUS_*` (article / image-download / image-gen / tts / video-assembly /
storage / zapcap-kept-no-captions / internal).

## Settings (rule 15)

Exposed: `SETTING_ROW_TIMEOUT_ONE_CLICK_IMAGE_VID` (operator-tunable row timeout,
consistent with every other tab). Per-row knobs live in sheet columns (Voice Over,
ZapCap, Change Size, Script Pattern, CTA/CTA Text, Open Comments). Intentionally
NOT exposed as settings: number of images (fixed at 4 = the 2×2 grid), atempo,
min-length floor — these are correctness constants, not preferences; promoting
them now would be premature knobs. Story prompt lives in code (not a settings
override) for v1; can be promoted to a settings-store prompt later if Evgeny wants
to tune the arc, mirroring the cartoon planner-prompt pattern.

## Testing (rule 18)

`tests/unit/test_one_click_image_vid.py`: payload round-trip (RowIn → Row →
payload → Row), dispatch routing (`isinstance` → correct processor), sheet reader
column mapping, single-video slot output shape, VO-length → `total` / `per_clip`
timing math, no-VO path skips ZapCap, `use this script:` override reaches the
script, story-prompt selection (`story=True` → narrative message, no baked text),
settings registered. Stubs Rendi / KIE / ZapCap / TTS adapters (each has its own
suite). Gate: full unit suite green, `ruff` + `mypy` clean, `node --check` on
Code.gs. One real render watched before prod is trusted.

## Cost (rule 8)

Per row ≈ one `image_vo` video's image cost + a slideshow assembly:
nano-banana-2 collage $0.06, upscale ~$0.02, ~5 Rendi commands (4 stills + 1
concat) ~$0.05, Gemini TTS ~$0.05, ZapCap (~15s) ~$0.025, GPT vision+prompt+script
~$0.02, storage ~$0.005 → **~$0.23 per finished video**. One video per row
(cheaper than the N-video tabs). No new paid services.

## Deploy (rule 19)

Additive — cannot alter existing tabs. Flow: PR `one-click-image-vid` → `main`
(review + CI). Prod is HF Spaces via `git push hf main:main` AFTER merge, plus
`clasp pull`/diff-reconcile then `clasp push` for Code.gs (Apps Script deploys
separately from the container). Bulk Videos 2 runs its own Space (`hf2`) and needs
the same ship. NONE of these shared-state pushes happen without explicit Yoav
go-ahead; the worktree + branch are removed once the PR is merged/closed.
Rollback = revert the merge + re-push; Code.gs rollback = re-push previous.
```
