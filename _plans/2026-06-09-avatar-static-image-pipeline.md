# Avatar tab: switch to static-image background with avatar overlay

Date: 2026-06-09
Status: Approved (decisions confirmed in chat — all recommended options
chosen)

## Goal

Rewrite the `video with avatar` row processor to produce a video that is
visually like the simple-x4 tab output: a single static image (Manual
Image if provided, else kie text-to-image) held for the duration of the
TikTok avatar narration, with the avatar composited at bottom-left.
Drop the cartoon 2-shot plan + Seedance animation step entirely.

## User decisions

1. **Script source:** article→script flow shared with the simple / simple-x4 tabs (`script_gen.generate_script`). NOT the cartoon planner.
2. **Background image:** kie text-to-image when no Manual Image (single-image flow mirroring image-vo). Image-to-image with the Manual Image as seed when it IS set.
3. **Output duration:** match the TikTok avatar audio duration exactly (no fixed cap; no trimming).
4. **CTA + ZapCap:** unchanged (Yes/No columns drive optional pill + captions).

## Cost impact (per row, rough)

| Item | Today | After |
|---|---|---|
| Cartoon planner OpenAI | ~$0.002 | dropped |
| Script generation OpenAI | n/a | ~$0.002 |
| kie image-gen (was 2 shots) | ~$0.06 | ~$0.03 (1 shot) |
| Seedance image-to-video (2 × 4 s) | ~$0.30 | dropped |
| TikTok avatar (operator-paid) | $0 | $0 |
| Rendi (concat + overlay) | ~$0.015 | ~$0.008 |
| **Total** | **~$0.39** | **~$0.05** |

~87 % per-row cost reduction.

## New pipeline

1. **Article fetch** (Tavily → ScrapingBee) — unchanged.
2. **Language detect** + **open comments classify** — unchanged.
3. **Script generation** — `script_gen.generate_script` (same as simple / simple-x4). Output: one ~60–90 s narration script + language metadata.
4. **CTA overlay setup** (Yes/No) — unchanged.
5. **Parallel: background image generation + avatar narration**
   - Image: if `manual_image_url` set → `nano_banana_2_image_to_image` from the manual seed; else `nano_banana_2_text_to_image` from a `single_image_prompt(article, language, vertical, ...)`. One image only.
   - Avatar: TikTok Symphony `create_and_wait` with the script from step 3. Returns `preview_url` (mp4) + `duration_seconds`.
6. **Static-image composite** — new Rendi method `still_image_with_avatar_overlay`: holds the still image for `duration_seconds`, overlays the avatar video at bottom-left, uses avatar audio as the only audio track. One ffmpeg invocation, one Rendi command. Output: an mp4 of length = avatar duration.
7. **CTA composite** (if enabled) — existing `overlay_image_bottom_center` (or equivalent), unchanged. Non-fatal: failure ships without CTA.
8. **ZapCap captions** (if enabled) — unchanged. Non-fatal: failure ships without captions (`STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS`).
9. **Upload + Ready Video** — unchanged.

## Dropped code paths

- `generate_cartoon_plan` import + call.
- `seedance_image_to_video` import + the `_animate` helper + the gather/fallback bookkeeping for missing clips.
- The whole `_generate_images` 2-shot loop and its prompt-chaining.
- `concat_clips_with_audio` call (no concat — single static image).
- `AVATAR_NUM_SHOTS`, `SEEDANCE_DURATION_SHORT`, `SEEDANCE_RESOLUTION` constants.

## New / changed helpers

- `pipeline/image_prompt.py` — reuse existing single-image prompt logic if present; otherwise add a `single_avatar_image_prompt(...)` that returns a kie-friendly prompt string (no cartoon scene grammar).
- `adapters/rendi.py` — add `still_image_with_avatar_overlay(image_url, overlay_video_url, ...)`. Implements: ffmpeg `-loop 1 -i image -i avatar -filter_complex ...`. Returns the same `RendiVideoResult` shape the existing overlay method returns so the caller swap is a one-liner.

## Settings audit (rule 15)

No new settings. The existing `script_gen` settings (`script_system_prompt`, `kie_model`, etc.) already cover the script + image generation knobs. The avatar overlay geometry (`AVATAR_OVERLAY_WIDTH_FRAC`, `AVATAR_OVERLAY_MARGIN_PX`) stays as code constants; no operator-facing knob requested.

## Security (rule 13)

No new external surface. Same Tavily + OpenAI + kie + TikTok + Rendi + GCS calls as today; just fewer per row. No new auth flows, no new secret material. Token validation paths unchanged.

## Observability (rule 14)

Existing log namespaces (`bulkvid row`, `bulkvid tiktok_avatar`, `bulkvid kie`, `bulkvid rendi`) all already emit per-stage events. Add a single new `avatar_pipeline_v2` boot-time log line on first invocation so a future "wait, when did the pipeline change?" question is one grep away. New Rendi method gets its own `rendi_still_overlay_*` log group.

## Testing (rule 18)

- Update `tests/unit/test_row_processor_avatar.py` happy-path test to mock the new pipeline: 1 image + 1 avatar + 1 Rendi composite (no Seedance, no concat).
- Add: Manual-Image-set branch → image-to-image with the manual seed.
- Add: kie image-gen failure → row fails with `STATUS_IMAGE_GEN_FAILED`.
- Add: avatar generation failure → row fails with `STATUS_TTS_FAILED`.
- Update: video assembly failure path (now Rendi `still_image_with_avatar_overlay` instead of concat + overlay).
- Add: ZapCap optional path still works on the new pipeline output.
- Run the full unit suite after the refactor.

## Files touched

- `src/bulkvid/orchestrator/row_processor_avatar.py` (heavy rewrite).
- `src/bulkvid/adapters/rendi.py` (new method `still_image_with_avatar_overlay`).
- `src/bulkvid/pipeline/image_prompt.py` (single-image prompt helper for avatar tab if not already there).
- `tests/unit/test_row_processor_avatar.py` (rewrite to match new pipeline).
- `tests/unit/test_rendi.py` (new method coverage).

Apps Script + sheet columns: **no changes**. Same `AVATAR_COLS`, same
operator UX, same payload shape, same `Ready Video` write-back.
