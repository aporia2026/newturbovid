# paste text on img — new sheet tab

Date: 2026-06-09
Status: Approved (decisions in chat)

## Goal

New sheet tab "paste text on img" that takes the operator's manual image +
a free-text Text column and ships one video with the text overlaid in
heavy white (thick black outline) at the center of the image. VO comes
from the article-fetch pipeline just like the existing simple tab.

## User flow

1. Operator types Country / Vertical / Article URL / Manual Image URL.
2. Operator types **Text** (the headline to overlay).
3. Operator picks Voice Over, ZapCap, aspect ratio, script pattern.
4. Aporia Bulk Video → Generate selected rows.
5. Backend produces one video: cover-cropped manual image + centered
   white-with-black-outline text + VO + optional ZapCap captions.

## Reference design

User mockup (chat 2026-06-09): square Spanish real-estate photo with
"Casas embargadas: precios y oportunidades" overlaid in heavy white
Inter Black with a thick black stroke, centered both axes,
auto-wrapped to 2 lines.

## Decisions (chat confirmations)

- **VO pipeline = same as `simple` tab.** Article fetch → language detect
  → script gen → Gemini TTS. The Text column is ONLY the on-image
  overlay, never spoken.
- **Overlay style matches the mockup exactly.** Inter Variable Bold,
  white fill, ~7% font-size stroke width in black, centered, auto-wrap
  to ≤4 lines, auto-shrink to floor at 4% canvas height.

## Files touched

Backend
- `src/bulkvid/pipeline/text_overlay.py` (NEW) — `overlay_text_on_image_bytes`.
- `src/bulkvid/models/row.py` — `TextOnImgRow` dataclass.
- `src/bulkvid/orchestrator/queue.py` — `TAB_TEXT_ON_IMG`, dispatch in
  `_payload_to_row` and `payload_to_row`.
- `src/bulkvid/orchestrator/row_processor_text_on_img.py` (NEW) —
  copy of simple processor with text-overlay step inserted between
  manual image download and Rendi.
- `src/bulkvid/orchestrator/runner.py` — `_TAB_TEXT_ON_IMG`, dispatch
  in `_tab_for_row` and `_dispatch_to_processor`, timeout (reuses
  `SETTING_ROW_TIMEOUT_SIMPLE`, default 720s — same shape of work).
- `src/bulkvid/routes/jobs.py` — `TextOnImgRowIn`, `_build_text_on_img_row`,
  branch in `submit_job` for `tab_type=text_on_img`.

Apps Script
- `apps_script/Code.gs` — `TAB_TEXT_ON_IMG`, `TEXT_ON_IMG_COLS`,
  `_readTextOnImgRow`, `_validateTextOnImg`, tab-name detection
  ("text on img" / "paste text"), dispatch through `generateAllUnprocessed`
  + `_submitJobForRowNums` + payload-building branch.

Tests
- `tests/unit/test_text_overlay.py` (NEW) — every aspect ratio in
  `DEFAULT_DIMENSIONS_BY_RATIO`, blank-text branch, white+black pixel
  presence, horizontal overflow regression.

## Security / safety

- `text` is server-side coerced to ≤240 chars in `_build_text_on_img_row`
  (longer would auto-shrink past readability anyway).
- `manual_image_url` is validated to start with `http://` or `https://`
  before download — same guard as the simple tab.
- Pillow rendering runs in a thread pool (`asyncio.to_thread`) so a
  pathological input image can't block the event loop.
- No new attack surface vs the simple tab — same auth, same storage,
  same Rendi command.

## Observability

- `row_start` / `row_done` / `row_failed` lines include `tab=text_on_img`
  and `overlay_chars` for filtering in HF logs.
- `text_overlay_rendered` logs the canvas size, font size landed on,
  line count, and stroke width so we can spot auto-shrink edge cases.
- `text_overlay_skip_blank` fires when the operator left the Text cell
  empty (the row still ships, just with no overlay drawn).

## Testing plan

- Unit tests cover the renderer in isolation (above).
- Manual: paste an image + a long Spanish headline into the new tab and
  visually confirm the rendered output matches the mockup.
- Manual: leave the Text column blank and confirm the row ships a clean
  unmodified-image video instead of erroring.

## Out of scope

- Operator-tunable overlay colors (hardcoded white/black per chat
  decision). If a future row needs a non-white fill or a thinner outline,
  it's a 1-line constant edit in `text_overlay.py`.
- Non-Latin scripts use Inter Variable's built-in glyph coverage
  (handles Cyrillic, Greek, Vietnamese, Polish ł/ę). The renderer does
  NOT fall back to Anton/Heebo/Cairo like Template 3 — for this overlay
  the wider Inter weight reads better at large sizes anyway.
