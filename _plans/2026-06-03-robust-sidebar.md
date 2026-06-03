# Robust sidebar: correct runs + logs

Date: 2026-06-03
Status: approved (user directive: "I just want the sidebar to be extremely more
robust, show correct runs and logs")

## Goal

The sidebar must be trustworthy: never blank out on a transient backend hiccup,
always show what each job is actually doing per row, and let the user open the
logs for a run without leaving the sheet.

## Problems today

1. `Sidebar.html` `renderError` replaces the whole Active panel with a red
   "Error: HTTP 500" on ANY failed poll. One transient blip (a brief SQLite
   lock during an active run) wipes the view and looks like a crash.
2. Progress is job-level only (`completed/row_count`). A single-row job shows
   `0/1` for its entire ~90s run with no sign of life or which stage it is in.
3. No way to see logs from the sidebar (the only log view is the admin panel,
   which is cookie-gated and not reachable from Apps Script's token calls).
4. Backlog is invisible: queued jobs from earlier submissions drain silently,
   which read as "it started generating randomly."

## Changes

### Backend (token-gated, reuse `get_identity` + ownership check)
- `GET /jobs/{job_id}/rows` -> `{job_id, rows: [{row_num, status, error,
  video_urls}]}`. Backed by the existing `queue.list_rows()`.
- `GET /jobs/{job_id}/log?tail=200&row=` -> `{job_id, exists, lines: [...]}`.
  Backed by a new shared helper.
- `logging.py`: add `read_job_log_lines(job_id, *, row=None, tail=300)` and move
  `_format_log_line` here (logging.py already owns the per-job log files).
  Refactor `admin.py`'s `job_logs` to use it (de-dupe the inline path logic).

### Apps Script (`Code.gs`)
- `_fetchJson(path, opts)`: retry wrapper, up to 3 attempts on 5xx / network
  error with a short backoff. Route listJobs / killJob / submit / new calls
  through it so transient 5xx self-heal.
- `getJobRows(jobId)`, `getJobLog(jobId, rowNum)`.

### Sidebar (`Sidebar.html`)
- Keep a `_lastJobs` cache. On a failed poll, re-render from cache + a small
  "reconnecting" note; only after 3 consecutive failures show a soft warning,
  and STILL keep the last good data on screen. Never blank out.
- Adaptive poll: 3s while a job is active, 12s when idle.
- Per active job: expandable per-row list (Row N -> status, video link when
  done, error when failed) via `getJobRows`.
- "View logs" per active job -> `getJobLog` shown in a `<pre>`.
- Header line shows active job count + total queued rows so backlog is visible.

## Out of scope (flagged, not done here)
- The underlying intermittent 500 (shared-file SQLite over PA's network FS).
  Client retry hides transient blips; the durable fix is MySQL-on-PA or a VM,
  tracked separately.

## QA
- Unit tests for `/jobs/{id}/rows` and `/jobs/{id}/log` (owner ok, 404, 403).
- Manual: submit, watch per-row status advance, open logs, kill mid-run, force a
  transient error and confirm the panel does NOT blank.
