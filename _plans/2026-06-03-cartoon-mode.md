# Cartoon mode (animated, image-to-video)

Date: 2026-06-03
Status: Shipped on `cartoon-mode` branch (commit 563cb22). Iterating on VO length.
Owner: TurboVid

## Goal

Add a new Google Sheet tab/mode, **cartoon**, that produces short animated cartoon
videos from a news article. Unlike every existing mode (still image + voiceover),
cartoon mode generates illustrated scenes from text, animates them with kie.ai
Bytedance Seedance 1.5 Pro (image-to-video), stitches them into a single ~6-7s
clip, and lays a short voiceover over the top.

This is TurboVid's first **animated, multi-shot** pipeline.

## Requirements (settled with the user, 2026-06-03)

- **2 output videos per row** = two independent video "ideas" from the same article.
  Written to Ready Video 1 (col J) and Ready Video 2 (col K).
- Each output video is a **multi-shot sequence**: 2-3 separately generated cartoon
  scenes, each animated, stitched into one ~6-7s video.
- **No seed image.** Cartoon scenes are generated from text. The "Manual Image"
  column is ignored for this tab.
- **Art style**: flat, warm, semi-realistic digital cartoon illustration, matching
  the reference frames the user supplied (driver-in-car / person-at-laptop look).
  A fixed style preamble locks the look across every generation.
- **Generic / symbolic characters only.** No depiction of real, named people.
  (Editorial-risk decision, see Security & safety.)
- **Short voiceover** (~6-7s of speech) per output video, via Gemini TTS.
- **Optional ZapCap captions** (per the row's ZapCap column).
- Aspect ratio from the row's "Change Size" column (default 9:16).
- Default render resolution 720p; Seedance audio generation OFF (VO only).

## Constraints / verified facts

- Seedance 1.5 Pro (`bytedance/seedance-1.5-pro`) accepts durations of **4, 8, or
  12 seconds only** — there is no 6 or 7. Image input field is `input_urls`
  (0-2 images). `aspect_ratio` required; `resolution` optional (480p/720p/1080p).
  Same create-task/poll API as the existing kie image models.
- nano-banana-2 supports **prompt-only text-to-image** (no seed) and image-to-image
  (with `image_input`). ~$0.04 at 1K, ~$0.06 at 2K.
- Rendi supports multi-input `filter_complex` concat; the current `rendi.py`
  adapter has no concat command, so that is new (but well-supported) code.
- Gemini TTS has no fixed output length and reads slowly (existing code already
  speeds it up via `atempo=1.3`).

## Chosen approach

Per-row, for each of the 2 video ideas (default `NUM_SHOTS = 2`):

1. **Article fetch** (Tavily -> ScrapingBee), once per row, reused by both ideas.
2. **Script + shot plan** (gpt-5.4-mini): produce, per idea, (a) a short VO line
   (~6-7s when spoken) and (b) `NUM_SHOTS` scene descriptions that form a tiny
   beginning -> payoff sequence.
3. **Voiceover** (Gemini TTS) for the idea's VO line; measure real duration `D`
   after the 1.3x speedup. `per_shot = D / NUM_SHOTS`.
4. **Scene images** (nano-banana-2):
   - Shot 1: text-to-image (no seed) with the fixed cartoon style preamble.
   - Shots 2..N: **image-to-image conditioned on shot 1** so character, palette,
     and world carry forward. This is the consistency lever the council demanded.
5. **Animate** each scene image with Seedance 1.5 Pro, `duration=4`, `resolution=720p`,
   aspect from the row. (4 is the minimum; we trim down.)
6. **Assemble** (Rendi, new concat command): trim each clip to `per_shot`, concat
   in order, force the target aspect (cover + center-crop), overlay the VO audio,
   `-shortest`. Output one ~D-second MP4 that matches the VO exactly.
7. **Persist** to GCS/S3, optional **ZapCap**, write URL to the idea's Ready Video
   column.

### Why these specifics (council mitigations)

- **2 shots default, not 3**: 4s trimmed to ~3.5s keeps almost all motion, so the
  "cut mid-motion" failure mostly disappears. 3 shots stays configurable.
- **Image-to-image chaining for shots 2..N**: the only real lever against no-seed
  character drift. Generic/symbolic characters (the editorial choice) drift far
  less than specific faces, so the two decisions reinforce each other.
- **VO measured first, shots trimmed to fit**: no hardcoded 7s, no silent desync.

## Alternatives considered and rejected

1. **Single 8s clip per idea** (the LLM Council's recommended default). One image,
   one 8s Seedance clip trimmed to the VO length. Cheapest, most coherent, reuses
   the existing single-command Rendi path, no concat code. **Rejected by the user**
   in favour of multi-shot narrative. Kept documented as the fallback if multi-shot
   output quality disappoints, and as the per-shot failure fallback (see below).
2. **3 shots x ~2.3s default**. More narrative cuts, but heavier per-clip trimming
   (cuts mid-motion) and 50% more Seedance cost. Available as a per-row/admin
   override, not the default.
3. **Seedance generated audio** instead of Gemini TTS. Extra cost, less control over
   language/voice, and the existing multilingual TTS path is already built. Rejected.

## Security & safety

- **Editorial / legal risk** (flagged by the council, all three peer reviewers): 
  cartoonising real news about real people carries defamation, likeness/IP, and
  misinformation exposure, plus platform AI-labeling policy risk. Mitigation, per
  the user's decision: **generic/symbolic characters only — never depict named real
  people's faces.** Enforced in the scene-prompt builder's system prompt (instruct
  the model to use anonymous/representative figures and symbolic scenes).
  **No real brands/logos either (crucial):** the image model renders recognizable
  badges by default (a live run produced a Volkswagen logo + readable plate), so a
  `NO_BRANDING` clause is appended to every image prompt the model actually sees —
  generic unbranded vehicles/products, no logos/badges/emblems, blank plates — and
  the planner is told never to name a real manufacturer. A planner-only rule is not
  enough; the constraint must reach nano-banana-2 directly.
- **Cost guard**: cartoon rows are 3-5x existing modes. Add a per-batch cost cap
  check before enqueue and surface cartoon's higher per-row estimate in the
  submit confirmation. Respect existing runtime cost guards (Phase 5 admin).
- **No secrets in code**: Seedance reuses the existing `KIE_AI_KEYS` pool. No new
  credential.
- **Partial-shot failure**: if one shot's image or animation fails after retries,
  fall back to filling that shot's slot by extending the previous shot (hold/loop)
  rather than failing the whole row. If shot 1 itself fails, the row fails with
  `STATUS_IMAGE_GEN_FAILED` (consistent with existing modes).

## Cost model (refresh live before release)

Per output video, 2 shots @ 720p:
- 2 scene images (nano-banana-2 @ 1K): ~$0.08
- 2 Seedance 4s clips @ 720p (no audio): ~$0.14
- VO (Gemini TTS): ~$0.003
- Rendi concat: ~$0.01-0.02
- Subtotal: ~$0.24 per video

Per row (2 ideas): **~$0.48**, or **~$0.68** with ZapCap (+$0.10/video).
3-shot mode: ~$0.70/row, ~$0.90 with ZapCap.
For comparison, existing still-image modes run ~$0.10-0.20/row.

## Implementation plan (files)

Mirrors the existing per-mode pattern (see row_processor_image_vo.py).

1. **`adapters/kie.py`**: add `MODEL_SEEDANCE = "bytedance/seedance-1.5-pro"`,
   `COST_SEEDANCE_*` constants, and wrappers:
   - `nano_banana_2_text_to_image(...)` — prompt-only, no `image_input`.
   - `seedance_image_to_video(client, image_url, prompt, aspect_ratio, duration=4,
     resolution="720p")` -> `(video_url, cost)`.
   (Existing `nano_banana_2` already covers image-to-image for shots 2..N.)
2. **`adapters/rendi.py`**: add a concat template + helper
   `concat_clips_with_audio(clip_urls, audio_url, per_shot_seconds, aspect_ratio)`
   building a dynamic `-i` list + `filter_complex` (trim -> concat -> overlay audio
   -> force aspect, `-shortest`). Add a cost constant reuse.
3. **`models/row.py`**: add `CartoonRow` dataclass (cols A-I like Image-VO; 2 outputs)
   and any new `STATUS_*` if needed.
4. **`orchestrator/queue.py`**: add `TAB_CARTOON = "cartoon"`; wire `_payload_to_row`.
5. **`orchestrator/row_processor_cartoon.py`** (new): the per-row state machine
   above. Never raises; tracks `_Costs`; maps failures to `STATUS_*`.
6. **`orchestrator/runner.py`**: dispatch `CartoonRow -> process_cartoon_row`.
7. **`adapters/sheets.py`**: `_CartoonCols`, `read_cartoon_rows`, write-back start col.
8. **`routes/jobs.py`**: `rows_cartoon` on `SubmitJobIn`; handler branch.
9. **`pipeline/`**: new `cartoon_prompt.py` (style preamble + scene/shot-plan builder
   + VO line generation) and the generic-characters guardrail.
10. **Tests**: unit tests for the new kie/rendi wrappers (mocked), the shot-plan
    builder, VO-duration splitting math, and a row-processor happy-path + a
    partial-shot-failure path. Match the existing test layout.

## Open questions

- Exact 4s Seedance pricing at 720p vs 480p — confirm live before release; 480p may
  be good enough for cartoons and halves animation cost.
- Should the 2 ideas share scene images to cut cost, or stay fully independent?
  (Current plan: independent, per the user's "2 separate ideas".)
- Default `NUM_SHOTS` exposed in the admin panel (Phase 5) vs hardcoded 2 for now.

## Rollout

Phase 1: build behind the new tab; test on 1-2 rows end to end; eyeball on a phone.
Phase 2: tune the style preamble + shot-plan prompt from real output.
Phase 3: surface `NUM_SHOTS`, resolution, and cost cap in the admin panel.

## Iteration log

### 2026-06-03 — VO length backstop (fixes 2026-06-03 live-run overshoot)

Live run on 2026-06-03 produced idea 2 with a VO ~2s longer than the assembled
video (two 4s Seedance clips, capped at 8s wall-clock). Root cause: the planner
asked for ~12 words in a 7-18 range but applied **no hard cap in code**, and the
observed Gemini TTS rate after the 1.3x speed-up is ~1.5 words/sec for short news
lines — at 18 words that's ~12s of speech, well past the 8s video cap.

Fix (`pipeline/cartoon_prompt.py`):
- Tightened the prompt range: target 9 (was 12), 6-11 (was 7-18). 11 words ×
  1.5 wps ≈ 7.3s, safely under the 8s video cap with headroom.
- Added a deterministic backstop `_enforce_word_cap()` invoked in
  `_coerce_ideas()`: truncates any voiceover over `CARTOON_MAX_WORDS`, preferring
  the last sentence boundary inside the cap (no mid-thought cuts) and falling
  back to a clean word-boundary cut + terminal period. Logs
  `cartoon_voiceover_capped` whenever it fires so model drift is visible.
- Tests added: cap enforced via integration through `generate_cartoon_plan`,
  plus unit tests on `_enforce_word_cap` for under-limit, sentence-boundary,
  no-boundary fallback, and trailing-punctuation cleanup; constants-consistency
  guard. Full suite: 479 passing.

Outstanding: **needs a live run to confirm** videos now land at 6-7s in both
ideas. Unit tests prove the code does what it should; only a live row proves
the rate calibration is right. If the model still picks the upper end of the
range and lines feel rushed in TTS, consider dropping max to 10 or tightening
the system-prompt wording to "around 9 words (6-11), ideally short".

### 2026-06-03 — VO length re-tune (corrects the over-correction)

Live run #2 on 2026-06-03 (Wikipedia "Used car", US/automotive, 9:16) shipped
both ideas successfully at $0.474 / 4:47, status SUCCESS, **no overshoot**. But
videos came out 3.82s and 5.82s — under the 6-7s target. Backstop did not fire
(model stayed inside 6-11 words on its own); the prompt range itself was too
tight.

Measured TTS rate from the run: ~1.75 wps after the 1.3x speed-up (7-word VO →
3.82s, 10-word VO → 5.82s). This recalibration shows that:
- The **old max=18** was the real overshoot driver (18 × 1.75 ≈ 10.3s, ~2.3s
  past the 8s ceiling — matches the original observation).
- The **old target=12** was actually correct (12 × 1.75 ≈ 6.9s, dead-centre).
- The **first-iteration target=9** was the source of the new undershoot.

Fix (`pipeline/cartoon_prompt.py`):
- Target 11 (was 9), range 9-13 (was 6-11). 11 × 1.75 ≈ 6.3s (centre of band),
  13 × 1.75 ≈ 7.4s (still under the 8s cap with margin).
- Comment in `cartoon_prompt.py` re-grounded on observed wps (was estimated).
- Constants-consistency test relaxed: max ≤ 14 (was ≤ 12).

User confirmed cartoon style itself looks right — only length needed adjusting.

### 2026-06-03 — Structural fix: video duration clamped to [6, 8]s

Live runs #2 and #3 showed the underlying problem is **TTS rate variance**, not
word count. Same model + voice produced 1.5-3.0 wps depending on the
model-generated `style_direction` (calm/deliberate → slow; punchy/excited → fast).
Run #3:
- Idea 1: ~11 words → 3.64s effective (3.02 wps, undershoot)
- Idea 2: ~13 words → 8.50s effective (1.53 wps, overshoots 8s cap by 0.5s)

Word-count tuning can't fix a 2x rate range. So the row processor now controls
the *video* duration directly rather than letting it track the VO.

Mechanism:
- `row_processor_cartoon.py`: new `TARGET_VIDEO_MIN_SECONDS=6.0` /
  `TARGET_VIDEO_MAX_SECONDS=8.0`. Every cartoon video is clamped to that band:
  `target = clamp(effective_vo, 6, 8)`, `per_shot = target / NUM_SHOTS`. New
  `cartoon_vo_sized` log records raw/effective/target/per_shot/clamped for every
  idea, so a future rate drift is visible without re-instrumenting.
- `rendi.py::render_cartoon_concat_command` gains an optional
  `total_video_seconds` kwarg. When set: replaces `-shortest` with
  `-t {target}` and adds a 0.3s `afade=t=out` on the audio. Short VOs leave
  trailing silence; long VOs cut cleanly via the fade. Legacy `-shortest` path
  preserved for any callers that don't pass it.
- `concat_clips_with_audio` forwards `total_video_seconds`; the cartoon row
  processor passes it.

Side effect: the word-cap/wps tuning is now belt-and-braces. Either lever
could regress and the video still lands in 6-8s. The cap prevents wasted TTS
chars; the wps tuning keeps natural-sounding lines that rarely hit the audio
fade.

Tests (6 new, full suite 485 passing):
- Rendi: `-t` + `afade` present with `total_video_seconds`; absent without it;
  silent path unaffected; short target clamps fade-start to 0.0 (no negative
  values).
- Row processor: short VO clamps to 6.0s, long VO clamps to 8.0s, in-band VO
  follows natural duration, no-VO defaults to 7.0s.

Outstanding: live re-run to confirm the assembled MP4s now land in [6, 8]s in
practice. Same Wikipedia "Used car" row.

### 2026-06-03 — Soft floor + longer VOs (silence-padding fix)

Live run #4 confirmed the hard 6s floor worked but produced 3s of trailing
silence on the short-VO idea (effective 3.15s in a 6s video). User flagged
that as "too much silence" — the structural clamp solved overshoot but
introduced a UX regression at the floor end.

The conflict is fundamental: hard floor → silence; no floor → 3s videos. The
fix uses *both* levers — push VOs longer at the source AND replace the hard
floor with a small dwell.

Mechanism:
- `pipeline/cartoon_prompt.py`: word range up. Target 13 (was 11), 11-15 (was
  9-13). At the observed wps spread (1.5-3.5 post-speedup), 13 words ≈ 6.5s
  at the median, 15 words still ≈ 4s at the fast end (which the soft tail
  rounds out).
- `orchestrator/row_processor_cartoon.py`: floor lowered to 4.0s. Target is
  now `clamp(effective_vo + VO_TAIL_SECONDS, 4.0, 8.0)` where `VO_TAIL_SECONDS
  = 0.8`. Short VOs get a deliberate ~0.8s dwell on the last scene; clamping
  to the floor only happens when effective_vo < 3.2s, and even then the dwell
  is ≤ 1.3s.
- The `cartoon_vo_sized` log keeps emitting raw/effective/target/per_shot/
  clamped so we can see in production whether the dwell is engaging or the
  floor is biting.

Why this beats the alternatives:
- "Just drop the floor" → 3-4s videos felt too short (user confirmed earlier).
- "Constrain style_direction" → root-cause fix for TTS rate variance, but
  bigger change. Held in reserve if the soft tail still leaves dead air.
- "Two VO lines per idea" → real architectural change. Deferred.

Tests (12 cartoon-related, full suite 485 passing):
- Row processor: 3.5s raw VO clamps to 4.0s floor; 13s raw clamps to 8s
  ceiling; 7s raw lands in-band at effective + tail; no-VO path unchanged.
- Word constants: max relaxed to ≤ 16 (the [4, 8]s clamp is the real bound).
- Existing rendi `-t` / `afade` tests unchanged — the command shape is the
  same, only the row processor's target arithmetic moved.

Outstanding: live re-run to confirm the new tail feels like a dwell rather
than dead air, and that the average video length now sits in the 5-7s range
naturally rather than at the 6s floor.

### 2026-06-04 — Planner robustness + log fix (pre-merge polish)

Live run #6 confirmed the soft-tail / longer-VO change: 8.0s and 6.3s videos,
both in band, ~0.3s audio fade on idea 1, ~0.8s dwell on idea 2. User
confirmed both feel right. Two follow-ups landed before the merge to main:

1. **`clamped` log field replaced with `clamp` ("floor" | "ceiling" | "none").**
   The boolean only checked raw `effective` vs band; it read `False` even when
   the `effective + tail` natural target hit the ceiling. The new tri-state
   field reflects what actually happened to the natural target, so future
   tuning has accurate signal.

2. **`_coerce_ideas` made permissive (the 2026-06-03 run #5 fallback bug).**
   That run hit `cartoon_plan_incomplete_filled got=0 wanted=2` — the planner
   returned valid JSON but `_coerce_ideas` rejected every idea. Both ideas
   fell through to the generic "Here's what you should know about X today"
   fallback and shipped byte-identical VOs.

   Fix: the coercer now tolerates common shape drift —
   - voiceover under voiceover / voice_over / vo / line / script / narration
   - shots under shots / scenes / sequence (and shots can be bare strings)
   - scene under scene / description / visual / image / prompt
   - motion under motion / action / animation / movement
   - shot lists too short are padded by repeating the last valid shot
     (image-to-image chaining downstream keeps the visual cohesive)
   - shot lists too long are trimmed to num_shots
   The generic fallback still fires when nothing usable can be salvaged.

   New `cartoon_idea_rejected` debug log records the reason per rejected idea
   (not_a_dict / no_voiceover / no_valid_shots) plus the model's top-level
   keys, and `cartoon_plan_incomplete_filled` now carries a `raw_preview` of
   the first 300 chars of the model response so the failure mode is visible
   in production without re-running.

Tests (+3, full suite 488):
- alt voiceover/scene/motion keys round-trip cleanly (no fallback)
- bare-string shots get default motion supplied
- single-shot lists pad by repeating last shot (both shots == padded copy)

### 2026-06-04 — Adaptive Seedance 8s for the last shot (VO completion fix)

First production run on cartoon-mode revealed the structural fix's blind spot:
even with the 0.3s audio fade, VOs whose `effective` exceeded 7.2-7.5s would
be hard-cut or fade-clipped on the LAST spoken word because the 8s soft
ceiling (= per_shot 4s × NUM_SHOTS 2) couldn't accommodate the audio. The
fade made the cut softer, not absent. User confirmed two production videos
both had this issue.

Root: per_shot × NUM_SHOTS = 8s is the wall-clock cap when every shot is a 4s
Seedance clip. Anything longer than that would either be truncated by `-t` or
faded by `afade=t=out`.

Fix: the LAST shot is now rendered at Seedance 8s when `effective_vo > 7.5s`,
so the video can extend up to a new TARGET_VIDEO_HARD_MAX_SECONDS = 11.0
(= 4s first shot + 7s last-shot trim). Earlier shots stay at 4s. Per-clip
trim durations are now PER-SHOT, not uniform.

Mechanism:
- `adapters/kie.py`: new `COST_SEEDANCE_PRO_720P_8S_USD = 0.14`.
  `seedance_image_to_video` now returns the cost matching the duration tier
  (4s → 0.07, 8s → 0.14) instead of always returning the 4s cost.
- `adapters/rendi.py::render_cartoon_concat_command` accepts
  `per_clip_seconds: float | list[float]`. List form trims each input clip to
  its own duration; the float form (uniform) is preserved for any
  non-cartoon caller.
  `concat_clips_with_audio` forwards the new shape.
- `orchestrator/row_processor_cartoon.py`: new constants
  `SEEDANCE_DURATION_LONG = 8`, `LONG_AUDIO_THRESHOLD_SECONDS = 7.5`,
  `TARGET_VIDEO_HARD_MAX_SECONDS = 11.0`. After TTS, if effective > 7.5,
  the LAST shot's `seedance_durations[s] = 8` and its trim fills the rest of
  the natural target (capped at both the 8s clip length and the hard max so
  Rendi never renders a clip whose tail will be truncated by `-t`). The new
  `cartoon_vo_sized` log adds `seedance_durations`, `per_clip_seconds`
  (list), `long_audio` (bool), and the clamp tri-state so production runs
  expose which path fired.

Cost impact: +$0.07 per idea ONLY when the long-audio path fires (~$0.14 per
row when both ideas hit it). Normal-VO rows are unchanged at ~$0.47. Worst
case (both ideas long): ~$0.61.

Tests (+4, full suite 492 passing):
- short VO uses 4s shots and uniform per_clip (regression cover)
- long VO (13s raw, ~10s effective) triggers per-idea `[4, 8]` Seedance
  durations and per_clip `[4.0, ~6.8]`; total video runs the full natural
  length (no audio cut)
- very long VO (16s raw, ~12.3s effective) clamps the LAST shot trim to 7s
  (4 + 7 = 11s hard max) so Rendi doesn't render the wasted second
- no-VO rows still use uniform 3.5s shots
- rendi command builder accepts per-clip list, raises on length mismatch

Outstanding: live re-run to confirm the long-audio path produces VOs that
finish before the video ends, and that the 8s last-shot Seedance billing
matches our $0.14 estimate.
