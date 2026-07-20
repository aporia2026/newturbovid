# google-simple-motion tab

Date: 2026-07-20
Status: APPROVED (decisions locked with Yoav 2026-07-20) — implementing
Author: Claude + Yoav

## Goal

A new `google-simple-motion` Sheet tab: a fixed-script "learn more about X" motion
ad generated from an article, produced in **1-4 size variants per row**. Each
variant is one realistic source image animated to fill a shared, localized fixed
voiceover, optional CTA pill + ZapCap captions, written to its own Ready Video
column.

## Column map (NEW multi-video layout — screenshot 2026-07-20)

```
A Country | B Vertical | C Article | D Manual Image 1 | E Manual Image 2 |
F Number of Videos (1-4) | G Voice Over | H ZapCap |
I Change Size 1 | J Change Size 2 | K Change Size 3 | L Change Size 4 |
M Script Pattern | N CTA | O CTA Text | P Open Comments |
Q Ready Video 1 | R Ready Video 2 | S Ready Video 3 | T Ready Video 4
```

0-indexed: country 0, vertical 1, article 2, manual_image_1 3, manual_image_2 4,
num_videos 5, voice_over 6, zapcap 7, aspect_1 8, aspect_2 9, aspect_3 10,
aspect_4 11, script_pattern 12, cta_enabled 13, cta_text 14, open_comments 15,
**ready_video_start 16 (col Q)**. Distinct layout — NOT an alias of simple-motion.
The existing write-back already lays `video_urls[0..3]` into consecutive Ready
Video columns, so slot _i_ → Ready Video _i_ (Q..T) drops in.

## What a row produces

`N = Number of Videos` (F, clamp 1-4). For slot _i_ (1-based):

| Slot | Source image (shot 1)                              | Change Size |
|------|----------------------------------------------------|-------------|
| 1    | Manual Image 1 (D) as-is; blank → generated scene  | Change Size 1 (I) |
| 2    | Manual Image 2 (E) as-is; blank → generated scene  | Change Size 2 (J) |
| 3    | AI image, image-to-image using Image 1 as reference | Change Size 3 (K) |
| 4    | AI image, image-to-image using Image 2 as reference | Change Size 4 (L) |

- **All N variants share ONE creative:** one subject, one randomly-chosen opening,
  one localized fixed script, one voiceover WAV. Only the source image + aspect
  differ per slot. Script + TTS generated ONCE, reused across all N.
- Each video is normally **ONE shot** — the source image animated for the whole
  video (a pure single-image ad, matching "V1 = manual image 1"). A single
  Seedance clip caps at 12s and Rendi's `trim=duration` does NOT hold a frame past
  the source, so when a long voiceover pushes the video past ~11.9s a **second**
  shot is added: an image-to-image continuation chained on the source (the proven
  simple-motion "1 manual + 1 generated" path). That fills 11-15s+ with continuous
  motion, no freeze, no truncation. Shot count is decided once per row from the
  measured VO length (`GSM_SINGLE_SHOT_MAX_SECONDS`).
- Blank Change Size within 1..N → **default 9:16**. `normalize_aspect_ratio`
  already recovers time-cast cells like "09:16" (commit 3e75469).
- **Slot alignment on partial failure:** `video_urls` is length N with `""` for a
  failed slot, so Ready Video _i_ maps to slot _i_ (a failed size leaves its cell
  empty, others keep their slot). Row fails only if ALL slots fail.

## Fixed script (deltas from simple-motion)

- Template (translated to the article language, subject named both times):
  `"<Explore more|Learn more|Read more> about the [SUBJECT]. Discover key details
  and useful information about [SUBJECT]."`
- Opening chosen **randomly** among the three (in code, `random.choice`).
- `[SUBJECT]` = short accurate article-topic description (LLM, from col C).
- Localized faithfully — the LLM emits the two complete grammatical sentences per
  language (a string-slotted subject breaks case/gender in DE/AR/etc.).
- **3.0s of silence between the two sentences.** Gemini 2.5 TTS speaks any
  prepended markup, so synthesize each sentence separately and splice 3.0s of
  zero-PCM silence into one WAV (RMS-matched so the two synths don't jump in
  loudness). ZapCap transcribes the audio → captions both sentences across the gap.
- **Length floored at 11s, words never truncated** (`total = max(11.0, spoken +
  0.5)`, no hard cap; short subject keeps it near/under 15s).
- **Voice Over = No** → silent 11s motion video per slot (no TTS, no gap, no
  captions).

## Architecture — reuse, don't fork (council-reviewed 2026-07-20)

### Extend the shared verbatim builder (backward-compatible)

`orchestrator/pinned_cartoon.py :: build_pinned_cartoon_video` gains two OPTIONAL
params; defaults keep cartoon / yt-cartoon / simple-motion byte-identical:

- `prebuilt_vo: PrebuiltVoiceover | None = None` — `(wav_bytes, duration_seconds)`.
  When set (+ `voice_over`), skip internal TTS, upload this WAV, size against
  `duration_seconds`. Builder's `cost_tts` stays 0 (processor counted the synths).
- `min_video_seconds: float = MIN_VIDEO_SECONDS` — the floor, applied in BOTH the
  voiced path and the VO-off silent path. This tab passes `11.0`.

Guarded by a regression test: the three existing callers, given no new args, size
identically and still call `tts.synthesize`.

### New audio-gap util — `pipeline/audio_gap.py`

stdlib-only (`wave`, `io`, `array`; NO numpy, NO `audioop` — removed in 3.13,
target is py3.12+). `join_with_silence(wav_segments, gap_seconds, *,
match_loudness=True) -> (wav_bytes, duration_seconds)`: unwrap each WAV → PCM,
RMS-match later segments to the first (clamped, skip near-silent), concat
`[seg1][gap][seg2]`, re-wrap via `wrap_pcm_to_wav`, duration via
`pcm_duration_seconds`.

### New localized script generator — `pipeline/google_simple_motion.py`

One cheap gpt call: `generate_learn_more_script(client, *, article_body, language)
-> {subject, sentence1_variants:[explore,learn,read], sentence2, cost_usd}`, all
in `language`, subject grammatically integrated. Opening picked by `random.choice`
in the processor. Defensive JSON parse + generic fallback.

### New processor — `orchestrator/row_processor_google_simple_motion.py`

1. article fetch → language detect + reconcile.
2. classify open comments (planner scene CONTEXT only — the script is fixed, Open
   Comments does NOT override it) + resolve safety.
3. `generate_cartoon_plan(num_ideas=1, num_shots=2,
   planner_prompt_key=SETTING_SIMPLE_MOTION_PLANNER_PROMPT)` → 2 realistic scenes
   + style_direction (shared; scene[0] for blank-cell shot-1 bases, scene[1] for
   the chained shot-2, guides the AI-ref variations).
4. If `voice_over`: `generate_learn_more_script` → random opening → TTS
   sentence1 + sentence2 (same voice/lang/country) → `join_with_silence(3.0)` →
   shared `prebuilt_vo` (count both synth costs once).
5. Resolve N + aspects[i] (default 9:16). Resolve each slot's shot-1 base image at
   its aspect: manual as-is (slots 1/2), generated scene (blank), or
   `nano_banana_2_image_to_image(ref=Image1/2, REALISTIC_STYLE)` (slots 3/4).
6. Per slot, CONCURRENTLY: render CTA overlay at aspect_i (if enabled), then
   `build_pinned_cartoon_video(fixed_shots=True, image_style=REALISTIC_STYLE,
   shots=[base as-is, generated chained scene2], aspect=aspect_i,
   prebuilt_vo=shared_vo, min_video_seconds=11.0, cta_overlay_url, zapcap...,
   slug=f"{base}_v{i}")`.
7. Return `video_urls` length N (slot-aligned, "" on failure) → Ready Video Q..T.

### Standard tab wiring (checklist)

- `models/row.py` — `GoogleSimpleMotionRow` (num_videos:int, aspect_ratios:list[str]
  length 4, manual_image_1/2, voice_over, zapcap, script_pattern, cta_enabled,
  cta_text, open_comments). `aspect_ratios` is a plain list[str] → no custom
  hydration (unlike simple_x4's nested dataclass).
- `orchestrator/queue.py` — `TAB_GOOGLE_SIMPLE_MOTION = "google_simple_motion"`,
  import, `payload_to_row` branch, union types.
- `orchestrator/runner.py` — import processor + row, `_TAB_GOOGLE_SIMPLE_MOTION`,
  timeout default 1800s (up to 4 concurrent renders) + a settings key, `_tab_for_row`
  branch, dispatch branch.
- `routes/jobs.py` — `GoogleSimpleMotionRowIn` (num_videos coerced 1-4,
  aspect_ratios list), `rows_google_simple_motion`, `_build_google_simple_motion_row`,
  submit dispatch branch.
- `adapters/sheets.py` — `TAB_GOOGLE_SIMPLE_MOTION` import, `_GoogleSimpleMotionCols`
  (ready_video_start 16 = col Q), `_HEADER_ROWS_BY_TAB` entry, `read_processed_row_nums`
  branch, **`batch_write_video_urls` positional_fallback branch** (the silent-drop
  trap).
- `apps_script/Code.gs` — **`_detectTabType`: match `google-simple-motion` BEFORE
  the `simple-motion` rule** (line ~365; the name CONTAINS "simple-motion"),
  `TAB_GOOGLE_SIMPLE_MOTION` const, `GOOGLE_SIMPLE_MOTION_COLS` (new layout), add to
  `SEEDANCE_VIDEO_TABS`, `_readGoogleSimpleMotionRow` (Number of Videos + 4 Change
  Size + CTA/Open Comments, header-name aware), `_validateGoogleSimpleMotion`, all
  tabType ternaries, `rows_google_simple_motion`, `_rowCountForPayload_`.

## Build order (de-risked — Executor)

1. Routing (Code.gs detect order + backend routing test).
2. audio-gap util + tests.
3. Extend builder + snapshot/regression test.
4. Script generator + tests.
5. Processor.
6. Full tab wiring (sheets cols + write-back branch + its test, same commit).
7. Full test run + QA.
8. Live validation (`git push hf main`): a real render must confirm ZapCap
   captions across the 3s gap, Rendi floors to 11s without cutting words, VO=No
   silence, the shot-1→shot-2 cut reads cleanly, and accent/aspect/duration match
   the market. (ZapCap-across-silence + the cut can't be exercised by unit tests /
   paid APIs locally — eyeball on the live Space.)

## Cost (rule 8)

Per row: 1 small gpt (subject/script) + 1 planner gpt + 2 short TTS (all ONCE,
shared). Then per video slot: up to 2 image gens (AI-ref base + chained shot-2) +
2 Seedance + 1 Rendi concat (+1 if CTA) + 1 ZapCap. So a 4-video row is heavy
(~6-8 image gens, 8 Seedance, 4 ZapCap) — inherent to N=4. No new vendor/secret.

## Security & safety (rule 13)

Generated + AI-ref images keep `REALISTIC_STYLE` + `NO_BRANDING` + the planner
safety block (no real logos/plates/figures — memory
`no-real-brands-in-generated-images`). Subject is LLM-extracted from the article
(short-bounded), not operator free text. Manual images used as-is (operator's
own), downloaded via the existing size/timeout-bounded `download_image`. No new
attack surface; auth path unchanged.

## Rejected alternatives

1. Dedicated processor duplicating ~150 lines. Rejected (council): forked paid
   code rots; backward-compatible params + snapshot test are as safe, no dup.
2. SSML `<break>` / "[pause]" in TTS text. Rejected: Gemini 2.5 speaks markup.
3. String-slot `[SUBJECT]` into a translated frame. Rejected: breaks grammar in
   inflected languages; LLM emits full sentences.
4. Single Seedance clip per video. Rejected: can't fill up to 15s (Seedance ≤12s,
   Rendi won't hold past source) → freeze/black tail.
5. Generalize into a fixed-template tab family / BYO-audio platform now. Deferred.

## Open items to eyeball on the live Space

- ZapCap caption alignment across the 3s silence.
- The shot-1 → shot-2 cut on a single source image (gentle push-in → pan).
- Loudness continuity across the spliced gap (RMS match is the safeguard).
- The 11s floor's trailing beat (background keeps moving, should not read frozen).
