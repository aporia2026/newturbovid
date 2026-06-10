# Apps Script — Sheet Integration

The bulk team's Google Sheet runs this Apps Script. It adds a custom menu
that submits batches to the FastAPI backend and a live status sidebar.

Plan: [_plans/2026-06-02-aporia-bulk-video-tool.md](../_plans/2026-06-02-aporia-bulk-video-tool.md) §5 + §7 + §15 Appendix A.

## Files

| File | Purpose |
|---|---|
| `Code.gs` | Menu, row parsers, OAuth ID token flow, job submit, sidebar bridge |
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
