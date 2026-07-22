# paste text on img — skip images that already carry a headline

Date: 2026-07-22
Status: approved (chat 2026-07-22) — implementing
Tab: `paste text on img` (`text_on_img`) only. Other tabs untouched.

## Goal

When the team runs a large batch (500+ rows), some of the Manual Images in
column D are already finished creatives with a headline burned into the
pixels. Overlaying our own text on those produces double text. The backend
should look at every Manual Image with an OpenAI vision model and, when the
image already carries a real overlay headline, pass the image through
untouched to column K instead of composing a new one.

Fully automatic. No new sheet column, no new operator step, no Apps Script
change — the team's workflow does not change at all.

## Decisions (chat 2026-07-22)

1. **What counts as "already has text": a deliberate typeset overlay only.**
   A headline / caption band / CTA / sticker layered on top of the photo as a
   graphic element counts. Text that is physically part of the photographed
   scene does NOT: shop signage, street signs, license plates, product
   packaging and labels, documents, screens, clothing prints, small corner
   watermarks. Those rows still get our overlay.

2. **On skip, the image is re-hosted and OUR url goes to column K.** The
   source bytes are uploaded byte-for-byte unmodified (no resize, no
   re-encode, no overlay) under `bulkvid/text_on_img/asis/` and that stable
   `storage.googleapis.com` URL is written back. Reason: Facebook / IG source
   URLs expire and 403, and column K must stay a set of URLs we control
   (memory `manual-image-must-be-rehosted`). `Change Size` is deliberately
   NOT applied — "as is" means as is.

3. **The detector fails open.** Any error, timeout, bad JSON, or a
   low-confidence verdict is treated as "no overlay text" and the row runs
   today's normal compose path. Rationale below.

## The asymmetry that drives the design

The two error directions are not equally bad:

* **False positive** (we skip an image that had no headline) → the ad ships
  with no text at all. Silent. Nobody notices until it is live.
* **False negative** (we compose over an image that already had a headline)
  → visibly doubled text on that one row. Obvious on review, one re-run to
  fix.

So every ambiguous case must resolve toward composing. Three guards
implement that bias:

* The prompt enumerates scene-text categories as explicit NOT-overlay cases.
* The model returns a `confidence` and we **skip only on `high`**. `low` or
  anything unrecognised composes.
* Exceptions and unparseable output return "no overlay text", never raise.

## Model + cost

`gpt-5.4-mini`, `detail="high"`, `response_format={"type":"json_object"}`,
`temperature=0.0`, `max_tokens=200`.

Verified live against the project key on 2026-07-22 (not assumed):

* `/v1/models` lists `gpt-5.4-mini`, `gpt-5.4-nano`, `gpt-4o`.
* `gpt-5.4-mini` accepts `temperature=0.0` AND `response_format` on a vision
  message. (This also clears `template_selector.py`, which passes
  `temperature=0.0` to the same model — it is not silently failing.)
* Classified a synthetic clean photo as `false` and the same photo with a
  white-on-black-stroke headline as `true`.
* 768x512 image at `detail=high` = 492 prompt tokens.

Pricing from developers.openai.com/api/docs/pricing, fetched 2026-07-22:
gpt-5.4-mini $0.75 / $4.50 per 1M in/out. A real ad image runs ~1000-1600
prompt tokens, so **~$0.0013 per row, ~$0.65 for a 500-row batch.**

`gpt-5.4-nano` ($0.20/$1.25) would cost ~$0.17 per 500. Rejected: saving
$0.48 per batch is not worth any additional false-positive rate on a check
whose failure mode is a silently text-less ad. Note `gpt-4o` (today's
`MODEL_VISION`) is no longer listed on OpenAI's current pricing page; the new
call path uses `gpt-5.4-mini`, which is already this repo's workhorse.

## Files touched

Backend
* `src/bulkvid/adapters/openai_client.py` — new `MODEL_TEXT_DETECT`
  constant; `vision_describe` gains an optional `response_format`
  passthrough (pure additive, forwards to the existing `chat`).
* `src/bulkvid/pipeline/text_detect.py` (NEW) — `OverlayTextVerdict` +
  `detect_overlay_text`. Mirrors `template_selector.py`'s shape: strict JSON,
  hallucination guard, never raises into the row processor.
* `src/bulkvid/orchestrator/row_processor_text_on_img.py` — new Stage 2
  between download and compose. On a confident yes, upload the source bytes
  unchanged and return that URL; otherwise fall through to today's Stage 3/4.
  New `_asis_object_key` + `_sniff_image_type` helpers, `_Costs.vision`.

Tests
* `tests/unit/test_text_detect.py` (NEW) — verdict parsing, confidence gate,
  hallucination guard, fail-open on HTTP error / bad JSON / missing key.
* `tests/unit/test_row_processor_text_on_img.py` — two existing tests assume
  OpenAI is never called and that `cost_breakdown` has only `storage`; both
  now need the vision call. Plus new cases for the skip path.

Apps Script: **no change.** Detection is server-side and automatic.

## Security / safety

* No new external surface: same OpenAI key, same storage bucket, same auth
  as every other row. One extra call to an already-trusted vendor.
* Model output is never interpolated into a prompt, a filename, or a URL. It
  is read as two enum-ish fields (`has_overlay_text`, `confidence`) and the
  free-text `reason` is only ever logged, truncated to 200 chars.
* Pass-through upload is bounded by whatever `download_image` already
  accepts, so no new size/DoS surface versus the compose path.
* The image is sent to OpenAI as base64 rather than by URL. This is required
  for correctness, not just privacy: OpenAI's fetcher gets dropped by
  Facebook's ad endpoint on TLS fingerprint (memory
  `manual-image-must-be-rehosted`), so a URL hand-off would fail on exactly
  the sources the team uses most.
* Content type on the pass-through upload is sniffed from the actual bytes,
  never from the source URL's extension, so a mislabelled source cannot make
  us serve a wrong `Content-Type`.

## Observability

* `overlay_text_detect_*` log lines: `ok` (with verdict, confidence, reason,
  cost), `low_confidence`, `parse_failed`, `call_failed`.
* `row_start` unchanged; `row_done` metadata gains `already_had_text` (bool),
  `detect_confidence`, `detect_reason`, and `cost_breakdown.vision` so a
  batch can be audited after the fact from the HF logs.
* The two paths are distinguishable from the URL alone:
  `bulkvid/text_on_img/asis/...` vs `bulkvid/text_on_img/...`.

## Blank Text column short-circuits the check

If column E is empty there is nothing to draw, so there is no doubled text to
prevent and the detector has no decision to make. The processor skips the
call entirely: blank-text rows keep their exact pre-2026-07-22 behaviour (fit
to Change Size, no overlay) and cost nothing in vision tokens. Without this
guard a blank-text row could pass through at native size instead of being
fitted, changing output nobody asked to change.

## QA — results, not intentions

Automated (`pytest`, 1331 passing, whole suite green):
* Detector: both directions, confidence gate (low / missing / unrecognised /
  wrong case), non-bool flag, missing key, malformed JSON, JSON non-object,
  reason truncation, HTTP 500, 401, and a request-shape assertion that the
  call really carries the inline image and `response_format`.
* Processor: pass-through uses the `asis` key with byte-identical output,
  JPEG source keeps `image/jpeg`, Change Size is ignored on pass-through,
  a hedged verdict composes, a vision outage composes, blank Text makes no
  call at all.
* Regressions held: bad URL fast-fail, download failure, and "Rendi / TTS /
  ZapCap / article-fetch never called".
* `mypy` clean on all three changed modules. `ruff` adds one finding over
  baseline (UP017 `timezone.utc`), matching the repo's only convention —
  5 uses of `timezone.utc`, 0 of `datetime.UTC`.

Live against the real API, real photographs, on 2026-07-22:
* 7/7 correct on: two clean stock photos, a photo whose only text is a car
  badge, a cityscape, and three images produced by this repo's own
  `overlay_text_on_image_bytes` in Spanish, German and Thai.
* 0/5 false positives on the worst scene-text images available — Times
  Square, Piccadilly Circus, Shibuya Crossing, a convenience store and a
  supermarket interior. Every one returned "the words are signage in the
  photographed scene, not an added headline".
* Measured cost: $0.00157 average per image → **~$0.79 per 500-row batch.**

Still worth a manual look on the live Space: one row with a plain photo
(expect the overlay) and one with a real already-texted creative from the
team's own library (expect the identical image in K).

## Rollout

One repo, two HF Spaces. Deploy is `git push hf main` **and**
`git push hf2 ...`; `hf2` tracks a squashed single-commit snapshot rather
than full history. Verify each Space via
`https://huggingface.co/api/spaces/<id>` → `runtime.stage == RUNNING` and
`runtime.sha` equal to what was pushed (memory `deploy-process`).

## Rejected alternatives

* **A checkbox column the operator ticks.** Free and 100% accurate, but it is
  manual work on 500 rows, which is the exact thing being automated away.
* **Local OCR (pytesseract / OpenCV MSER) instead of a model call.** Free and
  offline, but OCR fires on every legible pixel — a shop sign and a headline
  are identical to it. It cannot make the one distinction the whole feature
  rests on.
* **Applying `Change Size` on the skip path.** Keeps a batch visually
  uniform, but a 1:1 source into 3:2 gains blurred bars, so the output is no
  longer the untouched creative the team handed us.
