# video with avatar — new sheet tab

Date: 2026-06-09
Status: Approved (decisions in chat)

## Goal

New sheet tab "video with avatar" that produces ~8-second videos with
two AI-generated scenes animated as image-to-video (4 s each), narrated
by a TikTok Symphony avatar composited at the bottom-left corner.

## User flow

1. Operator fills Country / Vertical / Article URL / Manual Image (optional)
   / Avatar ID / Voice Over / ZapCap / Change Size / Script Pattern /
   CTA / CTA Text / Open Comments.
2. Aporia Bulk Video → Generate selected rows.
3. Backend:
   * fetches article, detects language, classifies open comments
   * generates a 2-shot plan (scene A + scene B + script for avatar)
   * generates 2 images:
     - if Manual Image set: image-to-image with manual as seed
     - else: text-to-image from the plan
   * Seedance animates each image to a 4 s clip
   * TikTok Symphony API generates the avatar narration video from the
     script + operator's avatar_id
   * Rendi concatenates the 2 clips into an 8 s background
   * Rendi composites the avatar video at bottom-left (~30 % width,
     rounded rectangle, 40 px margin) using the avatar's audio
   * optional CTA pill at bottom-center (reuses the cartoon CTA helper)
   * optional ZapCap captions
   * upload → write back to Ready Video.

## Decisions (chat confirmations)

- **Avatar ID is per-row** (new column). A separate admin page at
  `/admin/avatars` lists available avatars (preview thumbnail, ID,
  gender, display name) by calling TikTok's avatar list API. Operator
  visits the page, picks an avatar, pastes the ID into the sheet.
- **Smart image strategy**: image-to-image if Manual Image set, else
  text-to-image (same as cartoon).
- **Medium overlay**: ~30 % canvas width, rounded rectangle, bottom-left
  with 40 px margin.

## Files touched

Backend
- `src/bulkvid/adapters/tiktok_avatar.py` (NEW) — TikTok Symphony API
  client: `create_task(avatar_id, script, …)`, `wait_for_result(…)`,
  `list_avatars()`.
- `src/bulkvid/orchestrator/row_processor_avatar.py` (NEW).
- `src/bulkvid/pipeline/avatar_plan.py` (NEW) — 2-shot planner (script
  + 2 scene descriptions + style direction). Reuses the cartoon-style
  preamble and brand-safety clause.
- `src/bulkvid/adapters/rendi.py` — add `overlay_video_on_video` method
  with audio routed from the overlay (avatar speaks, background mute).
- `src/bulkvid/models/row.py` — `AvatarRow` dataclass.
- `src/bulkvid/orchestrator/queue.py` — `TAB_AVATAR`, dispatch.
- `src/bulkvid/orchestrator/runner.py` — register `_TAB_AVATAR`,
  timeout (reuses cartoon's 1200 s — similar shape of work).
- `src/bulkvid/routes/jobs.py` — `AvatarRowIn`, `_build_avatar_row`,
  branch in `submit_job`.
- `src/bulkvid/routes/admin_avatars.py` (NEW) — HTML page listing
  available avatars with previews.
- `src/bulkvid/adapters/sheets.py` — `_AvatarCols`, register in writer
  + reader dispatch.

Apps Script
- `apps_script/Code.gs` — `TAB_AVATAR`, `AVATAR_COLS`,
  `_readAvatarRow`, `_validateAvatar`, name-based detection ("video
  with avatar" / "avatar"), dispatch.

Tests
- `tests/unit/test_tiktok_avatar.py` — adapter happy path + parse
  failure + timeout.
- `tests/unit/test_row_processor_avatar.py` — pipeline happy path,
  manual-image branch, both shots fail, avatar API fails.

## Pipeline timing budget

| Step                       | Wall-clock | Notes                          |
|----------------------------|------------|--------------------------------|
| Article + script + plan    | ~5–8 s     | 2–3 OpenAI calls, parallel     |
| Image gen × 2 (kie)        | ~20–40 s   | sequential per row             |
| Seedance × 2               | ~30–60 s   | parallel                       |
| TikTok avatar              | ~30–90 s   | parallel with kie + seedance   |
| Rendi concat + overlay     | ~10–20 s   | sequential                     |
| ZapCap (optional)          | ~30 s      | sequential                     |
| Storage upload             | ~5 s       |                                |

Hard ceiling: 20 min (`SETTING_ROW_TIMEOUT_CARTOON`).

## Cost (rule 8)

- TikTok Symphony API: per-second of generated video, billed by
  TikTok. Operator already has an account configured per chat. No new
  paid third party introduced — kie / Seedance / OpenAI / Gemini all
  reused from existing pipelines.

## Security / safety

- `avatar_id` is per-row text bounded to 64 chars (TikTok IDs are
  short alphanumerics).
- The TikTok access token comes from env var `TIKTOK_ACCESS_TOKEN`
  (operator already set this) — never logged, never written to the
  sheet, never embedded in any client-facing URL.
- The admin `/admin/avatars` page is gated by the existing identity
  middleware — only admin emails can list avatars.
- The avatar video URL TikTok returns is a signed CDN URL — we
  download it to our storage immediately so the row's Ready Video is
  permanent, not expiring.

## Observability

- `tiktok_avatar_submit` / `tiktok_avatar_poll_pending` / `tiktok_avatar_ok`
  log lines mirror the kie adapter's pattern.
- `row_start` / `row_done` / `row_failed` include `tab=avatar` and
  `avatar_id` for HF log filtering.
- `avatar_overlay_failed_kept_no_avatar` fires on a non-fatal overlay
  failure (the row still ships an audio-less background instead of
  failing entirely).

## Settings audit (rule 15)

Two new settings registered (defaults sensible — operator only edits
if TikTok changes their endpoints):
- `tiktok_create_url`
- `tiktok_get_url`
- `tiktok_avatar_list_url`

## Testing plan

- Unit: TikTok adapter (mocked HTTP), row processor (mocked clients),
  Rendi command renderer.
- Manual: paste a row with a known avatar_id from the admin page,
  confirm end-to-end output matches the design.

## Out of scope

- Rounded-corner overlay (sharp rectangle in v1 — ffmpeg's
  rounded-rectangle via `geq` is messy; will revisit if user asks).
- Streaming generation (we synchronously poll TikTok like kie /
  Seedance).
- Avatar voice tuning beyond the avatar_id pick.
