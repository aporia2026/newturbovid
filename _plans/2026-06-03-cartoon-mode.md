# Cartoon mode (animated, image-to-video)

Date: 2026-06-03
Status: Awaiting approval
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
