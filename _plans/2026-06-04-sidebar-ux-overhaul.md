# Sidebar UX overhaul — per-step status, auto-tailing logs, real polish

Date: 2026-06-04
Status: approved (Yoav picked "All three phases" + "Plain and informative" tone)
Owner: Yoav
Builds on: the now-working HuggingFace Spaces + Turso migration shipped
earlier today (`_plans/2026-06-04-migrate-to-hf-spaces-turso.md`)

## Problem

Now that the backend works, the sheet sidebar is the bottleneck of the UX.
Today it shows job-level state (queued / running / done) and a row-level
status that's one of `pending / processing / done / failed`. Inside
`processing`, a row goes through ~15 distinct pipeline steps that take
seconds to minutes each (article fetch → safety check → script → TTS →
image gen → video assembly → ZapCap → upload). The user has zero
visibility into which step is current. The placeholder text literally
says **"Waiting in queue…"** while the worker is mid-pipeline, which is
worse than useless — it's misleading.

Concretely what's broken:
1. Active-row card shows "working…" or "Waiting in queue…" with no
   information about what's actually happening.
2. Logs only show when the user clicks "View logs" and then has to
   scroll manually.
3. No elapsed-time counter, so the user can't tell whether a row is
   making progress or wedged.
4. No ETA, so a 30-second simple-tab row and a 12-minute cartoon look
   identical until they finish.
5. Errors are tucked into a small `<span>` that's truncated to 140
   chars; the actual cause is buried in the log.
6. Empty states (no jobs, no rows) say bland things like "No active
   jobs. Submit one from the menu." without guidance.
7. Visual hierarchy is flat — everything is `font-size: 12-13px`,
   `color: #5f6368`. Important things don't stand out.

## Goal

Make the sidebar tell you, at a glance, exactly what's happening in
each row. Lazy-user bar (rule 10): you should never have to click "View
logs" to find out whether a job is progressing — the card should make
it obvious. When a job fails, the reason should be readable inline
without copy-paste-into-a-text-editor.

## Approach (3 phases, each independently shippable)

### Phase 1 — Live pipeline visibility ⭐

The biggest UX win, ship first.

**Server side (`src/bulkvid/routes/jobs.py` + small helper):**

- Add a small `step_extractor.py` module with a dict mapping log event
  keywords → human step names. Example:
  ```python
  STEP_FROM_EVENT = {
      "article_tavily_submit":      "Fetching article (Tavily)",
      "article_scrapingbee_submit": "Fetching article (ScrapingBee)",
      "article_fetch_ok":           "Article fetched",
      "safety_detect":              "Safety check",
      "script_submit":              "Writing script",
      "script_ok":                  "Script ready",
      "tts_synthesize":             "Synthesizing voice",
      "tts_synthesize_ok":          "Voice ready",
      "kie_submit":                 "Generating image",
      "kie_poll_pending":           "Generating image",
      "kie_poll_ok":                "Image ready",
      "kie_poll_fail":              "Image filtered — retrying",
      "rendi_submit":               "Assembling video",
      "rendi_poll_pending":         "Assembling video",
      "rendi_poll_ok":              "Video assembled",
      "zapcap_submit":              "Adding subtitles",
      "zapcap_poll_pending":        "Adding subtitles",
      "gcs_upload":                 "Uploading",
      "gcs_upload_ok":              "Uploaded",
      "row_done":                   "Done",
      "row_failed":                 "Failed",
  }
  ```
- Add `extract_current_step(job_id, row_num) -> str | None` that
  tails the per-job log file (last ~50 lines), filters for entries
  for this row (look for `row=<N>` or rely on the order of events
  within `row_start` … `row_done`/`row_failed`), and returns the
  human step name for the LAST matching event.
- `JobRowOut` gains two fields:
  - `current_step: str | None` — human-readable name (None when not
    yet processing).
  - `started_at: str | None` — ISO timestamp from `row_queue.started_at`
    (the column already exists; we just don't surface it).

**Client side (`apps_script/Sidebar.html`):**

- Render `current_step` prominently inside the active job card, where
  "working…" used to be. Use a slightly bolder weight + a single-line
  spinner-style ellipsis animation (pure CSS, no animation library).
- Add a live elapsed counter next to the step:
  `Synthesizing voice · 00:08`. Updates client-side every 1 s from
  `Date.now() - new Date(started_at).getTime()`.
- Auto-open the log pane the moment a row transitions to `processing`.
  No "View logs" click needed for the common case.
- Auto-scroll log `<pre>` to bottom whenever new lines arrive.
- Add a tiny "Pause auto-scroll" toggle so a user inspecting an older
  line doesn't get yanked back to the bottom.

### Phase 2 — Polish

After Phase 1 lands, take the rough edges off.

- **Typography.** Variable weights (400 normal, 500 emphasis, 600 active
  status). Slightly larger numerals on Progress/Cost. Tabular figures
  via `font-variant-numeric: tabular-nums` so counters don't shimmy as
  digits change.
- **Empty states.** Replace "No active jobs. Submit one from the menu."
  with two-line guidance: one short summary + a hint, e.g.
  > *No active jobs.*
  > *Select rows in the sheet → Aporia Bulk Video → Generate selected.*
- **Color states.** Tighten the palette so "running" really pops:
  - queued: muted grey `#5f6368` (today)
  - running: brand blue `#1a73e8` + subtle pulse on the progress fill
  - completed: green `#1e8e3e` (today)
  - failed: red `#d93025` (today) + slightly larger error caption
  - killed: amber `#b06000` (today)
- **Error display.** When `row.status === 'failed'`, render the error
  in a small `<details>` block that's expanded by default for the
  newest failure, collapsed for archive. Add a "Copy error" button.
- **Card density.** Hide `Succeeded: 0` and `Failed: 0` when both are 0
  in an active 1-row job — they're zero by definition and just visual
  noise. Show them as soon as either is non-zero.

### Phase 3 — ETA from past-job medians

Once Phases 1+2 land, give people a sense of how long this will take.

- **Server side.** New `/jobs/eta-medians` endpoint (admin-gated, cheap)
  that returns: for each `tab_type`, the median elapsed_seconds of the
  last 50 successful rows. Computed from
  `row_queue.finished_at - row_queue.started_at` (already in the schema)
  for rows with `status='done'`.
- **Client side.** Sidebar fetches medians once at boot, caches in
  memory. Active-row card shows
  `current_step · 00:08 · ~3:30 est` where `est` is the median for that
  tab type. No estimate shown for `cartoon` until we have ≥10 successful
  rows (too variable).
- Hide ETA when the elapsed counter overruns 2× the median by some
  margin — at that point the ETA is wrong and would just confuse.

## Alternatives considered and rejected

1. **Push every pipeline step to a new `row_queue.current_step` column,
   updated by the worker as each step starts.** Cleaner data model but
   touches every pipeline adapter — high blast radius for a UX change.
   Parsing the log file gets us the same info with one new server
   helper.
2. **Server-sent events / WebSockets for live progress.** Apps Script's
   `google.script.run` doesn't support either; we'd need to fight the
   sandbox. Polling at 3 s active cadence (already shipped) is good
   enough.
3. **Replace the sidebar with a separate web app.** Heavier UX win in
   theory but the sheet integration is the whole point — users live in
   the sheet, the sidebar is the right surface.
4. **Animations / glassmorphism / gradients.** Rule 5. Would just make
   it look AI-generated.

## Security & safety (Rule 13)

- The step-extractor reads the per-job log file via the existing
  `read_job_log_lines` helper, which already sanitizes `job_id` against
  `..` / slashes. No new path-traversal surface.
- `current_step` is sourced from log event NAMES, not log content.
  Event names are a closed set (our own emitters). No user-controlled
  content is reflected to the sidebar.
- ETA medians endpoint is admin-gated, same as `/health/deep`. Returns
  only aggregate seconds, not job content.
- No new auth surface, no new write surfaces.

## Observability (Rule 14)

- Server-side: add `step_extracted` DEBUG log on each
  `extract_current_step` call (with row, event, step name). OFF by
  default in prod; turn on when sidebar shows wrong step text.
- Client-side: existing `console.info('[bulkvid <namespace>] ...')`
  pattern continues — add `[bulkvid sidebar] step rendered` /
  `[bulkvid sidebar] log auto-opened`.
- `/jobs/eta-medians` logs a single `eta_medians_request` with
  user_email and the returned medians (cheap).

## Settings audit (Rule 15)

- Existing constants in `Sidebar.html` stay (poll cadences, log tail
  size). One new one: `AUTO_SCROLL_DEFAULT = true` (the auto-scroll
  toggle's initial state). Not surfaced to the user as a setting; lazy
  default is fine.

## Testing (Rule 18)

- Unit: new `tests/unit/test_step_extractor.py` covers:
  - extracts the LAST matching event from a sample log tail
  - returns `None` for an empty log
  - returns `None` for a log with no matching events
  - handles the `row=N` filter so two rows on the same job don't bleed
    into each other
- Unit: extend `test_routes_jobs.py` with a poll-response test that
  asserts `current_step` and `started_at` are present in `JobRowOut`.
- Manual: end-to-end on the deployed Space — submit a row, watch the
  sidebar tick through "Fetching article" → "Writing script" → … →
  "Done" with the elapsed counter advancing every second.

## Rollout

1. Phase 1 — code + tests + commit + push. One HF rebuild.
2. Phase 2 — same.
3. Phase 3 — same.
4. After each phase, Yoav verifies on the live Space + the sheet
   sidebar and gives a thumbs-up before I start the next phase.
5. Apps Script `Code.gs` / `Sidebar.html` changes get deployed by
   pasting the new file content into the script editor and saving (no
   "Deploy" needed for bound scripts).

## Out of scope

- Mobile sidebar layout. Google Sheets sidebar is desktop-only in
  practice.
- Internationalization. The sidebar copy is English. The team reads
  English. Not worth the i18n overhead today.
- Per-user customization (font size, dark mode). Pre-mature.
- A "compact / expanded card" toggle. Listed in the question to Yoav
  but he picked the bigger package; might not need this at all once
  Phase 1+2 land.
