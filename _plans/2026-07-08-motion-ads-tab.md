# Motion_Ads tab

Date: 2026-07-08
Status: built + unit-tested (all layers) — pending a live smoke test on the Space

## Goal

A new sheet tab, **Motion_Ads**, that turns an article into a silent motion-ad
video plus ad copy. Per row:

1. Read the article (col C).
2. Generate a realistic still image sized to **Change Size** (col H, default 16:9),
   OR use the operator's **Manual Image** (col F) as-is when present.
3. Animate the image with a subtle push-in into a **12s, silent** MP4 (no
   voiceover, no CTA, no captions, no audio track of any kind).
4. Write the video URL to **Ready Video** (col J).
5. From the article, also write a **Headline** (col D, <= 60 chars) and a
   **Description** (col E, <= 80 chars) — appealing but ad-policy-safe, in the
   article's language.

## Column layout (as built by the operator — see the sheet screenshot)

| Col | Header        | Role   |
|-----|---------------|--------|
| A   | Country       | input  |
| B   | Vertical      | input  |
| C   | Article       | input (URL) |
| D   | Headline      | OUTPUT (text) |
| E   | Description   | OUTPUT (text) |
| F   | Manual Image  | input (optional URL) |
| G   | Apple         | input (Yes/No) |
| H   | Change Size   | input (default 16:9) |
| I   | Open Comments | input (context/directives) |
| J   | Ready Video   | OUTPUT (URL) |

## Decisions (confirmed with Yoav 2026-07-08)

- **Video model: Seedance 1.5 Pro, always 12s @ 720p.** Seedance only accepts
  4/8/12s clips, so 12 is the closest to the 15s cap; it is silent by default
  (no audio to strip), ~$0.21/row, and loops fine on Apple/Taboola. Grok
  Imagine 1.5 (true 15s) was rejected: ~$2.11/row (~10x) and it bakes in audio
  we would have to strip. Duration is FIXED at 12 ("make it always 12").
- **Copy + image context = everything**: article + Country + Vertical + Open
  Comments all feed the single copy/scene LLM call.
- **Apple = Yes -> generated image contains NO people** (no humans, faces,
  hands, body parts). Applies to GENERATED images only (we cannot control a
  pasted Manual Image).
- **All generated images are realistic** (REALISTIC_STYLE), never cartoon.
- **Headline <= 60 chars, Description <= 80 chars** (Yoav's original numbers;
  ignore the char counts on the Taboola/Apple spec slides).
- No fallback-still-image output column (not requested).

## Pipeline (row processor)

1. Article fetch (Tavily -> ScrapingBee), same as every tab.
2. detect_language -> reconcile_language(country).
3. resolve_safety(vertical) — a sensitive-apparel vertical (e.g. "Bras PR")
   still forces product-only / no-humans on the IMAGE independent of Apple.
4. ONE LLM call (motion_ads_copy) -> {headline, description, image_scene},
   grounded in article + country + vertical + open_comments, in the detected
   language. Char caps enforced defensively after the call.
5. Image:
   - Manual Image present -> download + re-upload (stable, Rendi-free URL).
   - else nano_banana_2_text_to_image(REALISTIC_STYLE + scene + NO_BRANDING
     [+ NO_PEOPLE if Apple] [+ safety block if matched], aspect, 2K).
6. seedance_image_to_video(image, subtle-push-in, aspect, duration=12,
   resolution=720p) — one silent clip.
7. Persist the clip to our storage (download + upload). No Rendi, no concat.
8. Return RowResult{ video_urls=[url], headline, description }. Copy is set on
   BOTH success and post-copy failure paths, so D/E land even if the video step
   fails; J only lands on success.

## The one genuinely new bit of plumbing: writing TEXT back to the sheet

Today the write path only ever writes video URLs to "Ready Video". Headline /
Description need text write-back. Minimal, surgical, header-driven:

- `RowResult`   gains `headline: str = ""`, `description: str = ""`.
- `_record_result_sync` serializes them (durability).
- `PendingWrite` gains `headline`, `description`.
- `runner._handle_row` copies them from RowResult into PendingWrite.
- `sheets.batch_write_video_urls` resolves the "Headline" / "Description"
  columns by header name (positional fallback D/E for motion_ads only) and
  writes them when present. Generic + guarded, so no other tab is affected.

## Files touched

Backend:
- `models/row.py`            — `MotionAdsRow`; `RowResult.headline/description`.
- `pipeline/motion_ads_copy.py` (new) — the copy+scene LLM call.
- `orchestrator/row_processor_motion_ads.py` (new) — the processor.
- `adapters/kie.py`          — `COST_SEEDANCE_PRO_720P_12S_USD`; bill 12s right.
- `orchestrator/queue.py`    — `TAB_MOTION_ADS`; payload hydrate; serialize copy.
- `orchestrator/runner.py`   — timeout tab; dispatch; PendingWrite copy fields.
- `orchestrator/sheet_writer.py` — `PendingWrite.headline/description`.
- `adapters/sheets.py`       — `_MotionAdsCols`; text write-back; processed rows.
- `routes/jobs.py`           — `MotionAdsRowIn`; `_build_motion_ads_row`; submit
                               branch; `SubmitJobIn.rows_motion_ads`.

Apps Script (`apps_script/Code.gs`):
- `TAB_MOTION_ADS`, `MOTION_ADS_COLS`, add to `SEEDANCE_VIDEO_TABS`.
- `_detectTabType` (name "motion_ads"/"motion ads"/"motion-ads").
- `_readMotionAdsRow` (header-first), `_validateMotionAds` (article required).
- Wire into `generateAllUnprocessed`, `_submitJobForRowNums`, checkExisting,
  `_rowCountForPayload_`, `showActiveModels`, skip in `applyOpenCommentsTips`
  (no voiceover here).
- `applyMotionAdsDropdowns` menu action -> Yes/No dropdown on the Apple column.

## Cost (per row)

- Manual-image row: ~$0.21 (12s Seedance) + tiny LLM/article.
- Generated-image row: ~$0.27 (+$0.06 nano-banana 2K).
- 12s Seedance cost constant set to $0.21 (linear from the verified
  $0.07/4s + $0.14/8s anchors = $0.0175/s); verify on the next live run.

## Security / safety

- Only the article URL is required; Manual Image URL is optional and is
  downloaded through the existing `download_image` guard (same as avatar /
  simple-motion). No new external surface.
- No-real-brands clause always applied to generated images; no-people clause
  on Apple=Yes; sensitive-apparel safety block on matched verticals.
- Copy prompt instructs ad-policy-safe output: no unverifiable claims, no
  clickbait, no sensational punctuation / ALL CAPS, no misleading urgency.
- Text write-back is header-driven and value-guarded, so it can never write to
  the wrong column or clobber input cells on other tabs.

## QA checklist

- Generated-image row (Apple=No): image realistic, 12s silent video, D/E filled.
- Apple=Yes row: image has no people.
- Manual-image row: pasted image animated as-is; D/E still filled.
- Sensitive vertical (Bras PR): no humans in the generated image.
- Non-English market (DE/NL/SE): copy + any scene text in the article language.
- Article-fetch failure: row fails cleanly, no half state.
- Copy generated but video fails: D/E still written, J empty, row FAILED.
- Round-trip: payload -> queue -> processor -> record_result -> sheet write.
