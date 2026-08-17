# Apps Script — Sheet Integration

The bulk team's Google Sheet runs this Apps Script. It adds a custom menu
that submits batches to the FastAPI backend and a live status sidebar.

Plan: [_plans/2026-06-02-aporia-bulk-video-tool.md](../_plans/2026-06-02-aporia-bulk-video-tool.md) §5 + §7 + §15 Appendix A.

## Files

| File | Purpose |
|---|---|
| `Code.gs` | Menu, row parsers, OAuth ID token flow, job submit, sidebar bridge, HuggingFace restart |
| `Sidebar.html` | Live job status — polls `/jobs/{id}` every 5 seconds |
| `appsscript.json` | Manifest: OAuth scopes + V8 runtime + Jerusalem timezone |

## Install (one-time, per spreadsheet)

1. Open the bulk team's spreadsheet
2. **Extensions → Apps Script** (opens the script editor in a new tab)
3. In the script editor:
   - Click **Project Settings** (gear icon, left sidebar)
   - Tick **"Show 'appsscript.json' manifest file in editor"**
4. Replace the default `Code.gs` content with this folder's `Code.gs`
5. Click **+ → HTML** → name it `Sidebar` → paste this folder's `Sidebar.html`
6. Open `appsscript.json` and replace it with this folder's contents
7. Save (Ctrl+S)
8. Return to the spreadsheet, refresh the browser tab
9. New menu **"Aporia Bulk Video"** appears

## First run — configure backend URL

1. **Aporia Bulk Video → Configure backend URL**
2. Enter the FastAPI backend URL (e.g. `https://yoavaporia-aporia-bulkvid.hf.space`)
3. Click **OK**

The URL is persisted via `PropertiesService.getScriptProperties()` so each
team member only needs to set it once per script.

## Daily use

### Generate selected rows
1. Select the row(s) you want to process (click row numbers on the left)
2. **Aporia Bulk Video → Generate selected rows**
3. Sidebar opens with live status

### Generate all unprocessed
1. **Aporia Bulk Video → Generate all unprocessed**
2. Confirmation dialog shows how many rows will be sent
3. Sidebar opens with live status

### Watch progress
The sidebar polls `/jobs/{id}` every 5 seconds. As rows complete, the
backend writes the Ready Video URLs back into the sheet — they appear in
real time without refreshing.

### Kill a running job
**Sidebar → Kill job button** → confirms → calls `/jobs/{id}/kill`.

### When something looks stuck
Work down the sidebar's recovery buttons in order. Each one disturbs more than
the last, so stop as soon as things look right.

1. **Refresh now** — the sidebar may just be showing a stale poll.
2. **Fix stuck jobs** — runs the backend's repair pass immediately instead of
   waiting for the automatic one. Safe to click as often as you like: every
   action is idempotent and none of them can lose a video. You get one plain
   sentence saying what changed, with the full detail behind **Details**.
3. **Stop all jobs** — cancels everything still waiting. Rows already rendering
   are aborted too.
4. **Restart the worker** — last resort. Restarts the HuggingFace Space that
   generates the videos. Queued rows resume by themselves; it takes about a
   minute to come back.

**Self-heal log** (collapsed section) lists problems the backend found and fixed
on its own, newest first. Worth opening after a few days away — it is the only
place that record survives, because HuggingFace keeps no container logs from
before a restart.

## Configure worker restart (one-time)

The **Restart the worker** button talks to the HuggingFace API *directly*, not
through our backend. That is deliberate: the moment you most need it is the
moment the backend is the thing that stopped answering, and an endpoint running
inside the stuck container cannot restart that container.

1. Go to **huggingface.co → Settings → Access Tokens → Create new token**
2. Pick **Fine-grained**, and grant **write** access to the ONE Space that runs
   the backend, nothing else
3. Copy the token
4. In the sheet: **Aporia Bulk Video → Configure worker restart**
5. Step 1 asks for the Space id in `owner/space-name` form
6. Step 2 asks for the token
7. It immediately checks the Space and reports its current state, so you find out
   now rather than during an incident

**Why the token must be fine-grained and Space-scoped:** Script Properties are
readable by anyone who can open this Apps Script project, which for a
sheet-bound script includes anyone with edit access to the spreadsheet. Scoped
that way, the worst a leak allows is restarting a Space that person can already
reach. A broad write token would hand over the whole account, so do not use one.

The token is never sent to our backend and never written to a log. To rotate it,
run **Configure worker restart** again and paste the new one.

Restarts are rate-limited to one per minute in the script. A restart takes
30-60 seconds to come back, and restarting a Space that is already restarting
only makes the outage longer.

**On mobile:** custom menus and sidebars do not appear in the Google Sheets phone
app, so this button is desktop-only. From a phone, restart the Space from its
page on huggingface.co instead. The backend's own automatic recovery does not
need you present either way.

## Authentication

`Code.gs` calls `ScriptApp.getIdentityToken()` to get a Google-signed JWT
identifying the active user. The backend:

1. Verifies the JWT signature against Google's JWKS
2. Checks the `hd` claim matches `aporia.com` (Workspace domain)
3. Checks `email` is in `BULK_TEAM_ALLOWLIST` (or `ADMIN_ALLOWLIST`)
4. Returns 401 / 403 on failure

No shared secret lives in the script — every team member authenticates as
themselves. Revocation is per-user (remove from allowlist on the backend).

## Tab autodetection

The script detects which tab you're on by reading row 1 (the header):

- Header includes **"Manual Image"** → Image-VO tab
- Header includes **"How Many"** → 4Images-VO2 tab
- Neither → polite error

This means the script works on any spreadsheet that follows the column map
from plan §15 Appendix A, not just the original `video-pj`.

## Troubleshooting

**"Could not get Google OAuth ID token"** — the script needs to be
re-authorized. Open the script editor, click **Run → Run function**
(any function), accept the OAuth prompts, then try the menu again.

**HTTP 401 from backend** — your email isn't in the allowlist yet. Ask
Yoav to add it.

**HTTP 403 from backend** — your email is in the allowlist but your
Workspace domain doesn't match `aporia.com`. Confirm you're signed in as
your Aporia account.

**Job stuck in "queued" forever** — the worker isn't running. Check the
worker logs on the host (PythonAnywhere always-on task / Hetzner Docker).

**A job shows "running" but its videos are already in the sheet** — click
**Fix stuck jobs**. This was a real bug (a lost database write left the job's
progress counter one short, so it could never finish); the backend now repairs it
automatically within a few minutes, and the button does it on the spot.

**"Worker restart is not set up yet"** — run **Configure worker restart** first,
see the section above.

**"HuggingFace refused the token"** — the token expired, or it lacks write
access to that Space. Create a new fine-grained token and run **Configure worker
restart** again.
