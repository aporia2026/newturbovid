# simple-motion tab

Date: 2026-06-22
Status: APPROVED (geometry + story-source decided with Yoav 2026-06-22) — implementing
Author: Claude + Yoav

## Goal

Add a new `simple-motion` Sheet tab: a variant of the `cartoon` tab that animates
**super-realistic** images (not cartoons) and lets the operator supply their own
images in columns D / E. One 8-second video per row (two 4s shots stitched), with
an article-driven voiceover.

## Resolved decisions (Yoav 2026-06-22)

1. **Video geometry: ONE 8s video per row** (2 shots × 4s). Column D = shot 1's
   image, Column E = shot 2's image. A blank column gets an auto-generated
   super-realistic image. Writes back to **Ready Video 1** only; Ready Video 2
   stays empty. (Chosen over "two videos, one per image" and "two cartoon-style
   videos" — closest to "2 images, 4s each, like the cartoon tab".)
2. **The article tells the story.** Voiceover is written from the Article (same
   planner as cartoon). No vision model reads the pasted images.

## Manual-image resolution (the new logic vs cartoon)

Per shot `s` (0 = col D, 1 = col E):

| D (shot 1) | E (shot 2) | shot 1 image            | shot 2 image                          |
|------------|------------|-------------------------|---------------------------------------|
| blank      | blank      | text→image (realistic)  | image→image chained on shot 1         |
| set        | blank      | D as-is (download+reup) | image→image chained on D              |
| blank      | set        | text→image (realistic)  | E as-is (download+reup)               |
| set        | set        | D as-is                 | E as-is                               |

- Manual images are used **as-is** — downloaded and re-uploaded to our storage so
  the URL is stable + Rendi-reachable (avatar-tab precedent
  `_resolve_background_image`). No AI rewrite, no aspect coercion.
- Generated images use a new `REALISTIC_STYLE` preamble + the existing
  `NO_BRANDING` clause (no real logos/plates — memory `no-real-brands…`).
- Chaining shot 2 on `image_urls[0]` carries the look across the cut, exactly like
  cartoon — and anchors a generated shot 2 to a pasted shot 1.

## Voiceover + motion

- **Voiceover**: article-driven, calm/factual, one line (~10 words), sized to the
  flat 8s window. Reuses the cartoon planner + `compute_atempo` + shorten-and-retry
  exactly (1 idea instead of 2).
- **Motion**: a *generated* shot uses the planner's scene-matched motion; a *manual*
  shot uses a universal gentle cinematic push-in (the planner can't see the photo).

## Realistic planner prompt

`generate_cartoon_plan` already accepts `planner_prompt_key` / `planner_prompt_default`
(yt-cartoon uses this to swap prompts without forking the planner). simple-motion
passes a new admin-editable `simple_motion_planner_prompt` — the calm cartoon prompt
reworded to describe **realistic/photographic** scenes (not "cartoon scenes"), so the
scene text doesn't fight `REALISTIC_STYLE`. Same JSON shape, same placeholders, same
brand-safety + complete-sentence rules. `num_ideas=1`, `num_shots=2`.

## Column map (matches the sheet Yoav already laid out)

```
A Country | B Vertical | C Article | D Manual Image 1 | E Manual Image 2 |
F Voice Over | G ZapCap | H Change Size | I Script Pattern | J CTA |
K CTA Text | L Open Comment | M Ready Video 1 | N Ready Video 2
```

Apps Script reads by HEADER NAME first (avatar/yt-cartoon precedent) with the
positional `SIMPLE_MOTION_COLS` as fallback. Write-back resolves "Ready Video 1"
by header (positional fallback = col M / 0-indexed 12).

## Architecture (variant, not a change to cartoon — same call as yt-cartoon)

New `SimpleMotionRow` + new `row_processor_simple_motion.py` that REUSES the shared
helpers (`generate_cartoon_plan` with the realistic prompt, `compute_atempo`,
`shorten_voiceover`, `render_cartoon_cta_overlay_bytes`, ZapCap, Rendi
`concat_clips_with_audio`). The cartoon + yt-cartoon orchestration is untouched.

### Files to touch

Backend: `models/row.py`, `orchestrator/row_processor_simple_motion.py` (new),
`orchestrator/queue.py` (tab constant + (de)serialize), `orchestrator/runner.py`
(dispatch + timeout + `_tab_for_row`), `routes/jobs.py` (`SimpleMotionRowIn` +
builder + dispatch), `pipeline/cartoon_prompt.py` (`REALISTIC_STYLE` +
`image_prompt_for_shot(style=…)`), `orchestrator/runtime_settings.py` (realistic
planner prompt + timeout setting), `adapters/sheets.py` (`_SimpleMotionCols` +
write/read-processed/header-rows maps).

Apps Script: `Code.gs` — `TAB_SIMPLE_MOTION`, `SIMPLE_MOTION_COLS`, detect
`simple-motion`/`simple motion` BEFORE `simple` (name contains "simple"),
`_readSimpleMotionRow` (header-name reads, 2 manual image cols),
`_validateSimpleMotion`, dispatch (`rows_simple_motion`), cols selectors.

Tests: cartoon-unchanged guard (`image_prompt_for_shot` default style byte-
identical), payload round-trip for `SimpleMotionRow`, manual-image resolution
matrix (the 4 cases above), realistic-style prompt assembly.

## Rejected alternatives

1. **Extend `CartoonRow` / `process_cartoon_row` with realistic + manual-image
   flags.** Rejected — threads two image sources + a style switch through the
   fragile flat-8s closure on a live, paid tab. yt-cartoon already proved the
   variant pattern is the safe move.
2. **Vision-describe the pasted images to tailor VO/motion.** Rejected by Yoav —
   the article tells the story; adds cost + an external dependency.
3. **Two output videos per row.** Rejected by Yoav — one 8s video; Ready Video 2
   left empty.

## Security & safety (rule 13)

- **Generated images**: `NO_BRANDING` clause stays (no real logos/plates/brands) +
  the planner's "generic, no real public figures, no legible on-screen text" rules.
- **Manual images**: used as-is — operator's content, operator's responsibility.
  Downloaded with the existing `download_image` (size/timeout-bounded) and
  re-uploaded to our storage; never trusted as a Rendi-reachable URL directly.
- **Input validation**: aspect/voice/zapcap/CTA coerced defensively (Apps Script +
  server), never 400 the batch. `cta_text` bound at 80 chars; manual image URLs
  passed through (same as avatar/image_vo).
- **No new secrets, no new vendor, no new attack surface.** Auth path unchanged.
- **Cost**: ≤ one cartoon idea per row (1 video, ≤2 generated images + 2 Seedance
  4s clips + 1 VO). When the operator supplies both images, image-gen cost is zero
  (only 2 storage re-uploads). Per-shot graceful degradation kept.

## Open questions for Yoav

1. Ready Video 2 (col N) stays empty — confirmed acceptable? (Following the chosen
   geometry.)
2. Realistic planner prompt default acceptable as v1 (admin-editable later)?
