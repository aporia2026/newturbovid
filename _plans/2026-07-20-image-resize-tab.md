# image_resize tab — generative reframe of an image to a new aspect ratio

Date: 2026-07-20
Author: Claude + Yoav
Status: built + tested (full suite green, mypy clean); pending deploy

## Goal

A client asked (via Yoav) for a way to change an image's size while keeping its
design and any baked-in text looking good. Yoav created a new sheet tab named
`image_resize`. The operator drops an image (with or without text) into
**Manual Image (D)**, picks a target size in **Change Size (H)**, and the tool
writes a resized image URL to **Ready Image (K)** that looks native at the new
aspect ratio, preserving the design and the text.

One image per row. No video, no voiceover.

## Decisions (locked with Yoav 2026-07-20)

1. **Approach: generative reframe**, not deterministic crop or a text-safe
   hybrid. Nano Banana 2 (image-to-image, already integrated) reframes the whole
   image to the target ratio, extends the background/design naturally, and
   re-renders the text. Chosen for "looks great at a real ratio change" with the
   least new code. See "Rejected alternatives".
2. **Text is baked into the image.** Column E ("Text") is inherited from the
   `text_on_img` layout and is **ignored** on this tab. We preserve whatever text
   is already in the pixels; we do not overlay operator-typed text.
3. **Resolution 2K** ($0.06/image) for text legibility, over 1K ($0.04).
4. **Blank Change Size is a clear soft error**, not a silent no-op. A resize with
   no target size is meaningless and we will not spend $0.06 to regenerate an
   image at its own ratio. The row reports "Change Size is required — pick a
   target size."

## Why generative (and the honest risk)

"Resize to a new aspect ratio AND keep the text perfect" is two goals that pull
against each other. A full generative reframe looks native at any ratio but
regenerates the whole frame, so it can nudge a German umlaut or Italian accent
(this client ships DE/IT/US — see memory `localization-quality-attention`). A
text-safe hybrid (original pixels untouched + generatively fill only the new
margins) guarantees the text but needs masked-outpaint support the kie adapter
does not currently expose, and looks less seamless.

Mitigation for the chosen path: a prompt that explicitly forbids changing the
text (same wording, same language, no translation/re-lettering, keep legible),
2K resolution, and a one-glance operator check on the Ready Image. If a future
client needs byte-perfect text, we add the hybrid as a per-row toggle then.

## Architecture (mirror the `text_on_img` tab — image in, image out)

`text_on_img` is the template: validate manual image → download → compose →
upload PNG → write URL to Ready Image via `RowResult.video_urls`. We swap the
compose step for a Nano Banana reframe.

### Pipeline (`orchestrator/row_processor_image_resize.py`, new)
1. Validate Manual Image URL (D) is http(s).
2. Validate Change Size (H) is present and parseable. Blank → soft error.
3. `image_gen.edit_with_fallback(source_image_url=D, prompt=REFRAME_PROMPT,
   aspect_ratio=<ratio>, resolution="2K")` → Nano Banana 2, with GPT Image 2 +
   AtlasCloud as automatic fallbacks. Returns `(kie_url, cost)`.
4. Download `kie_url` (kie URLs are ephemeral).
5. If Change Size was pixels `WxH` → `crop_to_pixels` to the exact size; else
   run `optimize_image_for_size` (2 MB cap) at the model's ratio.
6. Upload to our storage (`bulkvid/image_resize/...`) → stable URL.
7. Write URL to Ready Image (K).

### Files touched (each mirrors an existing tab)
- `models/row.py` — add `ImageResizeRow` (mirror `TextOnImgRow`; `text` etc. kept
  for wire compat but ignored).
- `orchestrator/queue.py` — `TAB_IMAGE_RESIZE = "image_resize"`; `payload_to_row`
  branch + return-type union.
- `orchestrator/row_processor_image_resize.py` — new (above).
- `orchestrator/runner.py` — import processor + row; `_TAB_IMAGE_RESIZE`, timeout
  (reuse image_vo 900 s — real model call), `_tab_for_row` + dispatch branches.
- `routes/jobs.py` — import row + TAB; `ImageResizeRowIn`; `rows_image_resize`
  field; `_build_image_resize_row`; dispatch branch.
- `apps_script/Code.gs` — `TAB_IMAGE_RESIZE` + `IMAGE_RESIZE_COLS` (identical to
  `TEXT_ON_IMG_COLS`); `_detectTabType` rule; `_readImageResizeRow`; submit
  wiring (`rows_image_resize`); full Change Size dropdown (not the Seedance
  subset).
- `tests/` — mirror the `text_on_img` tests (row parse, object key, happy path
  with mocked `edit_with_fallback` + storage, blank-size soft error).

### Column map (identical to `TEXT_ON_IMG_COLS`)
A country, B vertical, C article (ignored), D manual image, E text (ignored),
F voice over (ignored), G zapcap (ignored), H Change Size, I script pattern
(ignored), J open comments (ignored), K Ready Image.

## Security / cost
- No new vendor or attack surface. Same operator-URL download path as
  `text_on_img` (through `url_guard`). No new secrets, no new PII logging.
- Cost: kie.ai Nano Banana 2, $0.06/image at 2K (existing, budgeted). 1,000
  images ≈ $60. Fallbacks (GPT Image 2 ~$0.08, AtlasCloud) only on failure.

## Rejected alternatives
- **Deterministic crop** (`crop_to_ratio`): free and instant but destroys
  content — crops away edges and cuts text/subject on any real ratio change.
  Fails the "keep the design" requirement.
- **Text-safe hybrid** (original untouched + generative margin fill): guarantees
  text but needs masked-outpaint support kie doesn't expose today, more code,
  less seamless. Held in reserve as a future per-row toggle.

## Deploy (from memory)
- Backend: `git push hf main` (manual; GitHub does not auto-deploy). Verify via
  the HF API `runtime.sha`.
- `Code.gs`: manual copy-paste into the Apps Script editor (not git). After
  deploy, run "Update size dropdowns on all tabs" and eyeball one live render.

## Open questions
- None blocking. Resolution and blank-size behaviour decided above.
