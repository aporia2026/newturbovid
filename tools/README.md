# tools/run_local.py — local runner for the bulk-video pipeline

A standalone CLI that processes Google Sheet rows on this machine instead of
PythonAnywhere. Use it when PA is flaky and you need to push a batch through
on your own laptop.

It is **the same pipeline** PA runs — same kie / OpenAI / Vertex / Rendi /
ZapCap / Tavily / ScrapingBee calls, same sensitive-apparel safeguard, same
admin-tuned prompts. It just skips the FastAPI HTTP layer, the SQLite job
queue, the worker daemon, and the Apps Script menu. Everything else is shared.

Design rationale: [_plans/2026-06-04-local-runner-script.md](../_plans/2026-06-04-local-runner-script.md).

---

## First-run setup (once per machine)

1. **Install Python 3.12** if you don't have it. Confirm:
   ```powershell
   python --version
   ```
   Expect `Python 3.12.x`.

2. **Clone the repo** somewhere convenient:
   ```powershell
   git clone https://github.com/aporia2026/newturbovid c:\Projects\turbovid-new
   cd c:\Projects\turbovid-new
   ```

3. **Create a virtualenv and install deps:**
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -e ".[dev]"
   ```

4. **Get your `.env` file** from Yoav (out-of-band — password manager, encrypted
   email, USB). Put it at the repo root: `c:\Projects\turbovid-new\.env`.

   **Never** commit it, paste it into Slack, or share it in chat.

5. **Get the Google service-account JSON file** from Yoav (same out-of-band
   channel). Put it somewhere on disk and make sure `SHEETS_SERVICE_ACCOUNT_FILE`
   in your `.env` points to that path. Example:
   ```
   SHEETS_SERVICE_ACCOUNT_FILE=C:\Users\you\secrets\turbovid-sa.json
   ```

5a. **Set the default spreadsheet** so you don't have to retype the sheet ID
    every invocation. Open the bulk-team Google Sheet, copy the long token from
    its URL (between `/d/` and `/edit`), and add it to your `.env`:
    ```
    BULKVID_DEFAULT_SHEET_ID=1AbC...XyZ
    ```
    From here on, every `python tools/run_local.py` will use that sheet
    unless you override with `--sheet-id`.

6. **Optional — settings parity with PA.** If Yoav has tuned prompts or the
   sensitive-apparel keyword list on PA's admin panel, ask him for a copy of
   `settings.db`. Drop it into the data dir referenced by `BULKVID_DATA_DIR`
   (default `./data/settings.db`). Without this step you'll use the built-in
   defaults from `runtime_settings.registry_defaults()` — fine for testing but
   not a 1:1 production match.

7. **Smoke test** without calling any vendor (assumes step 5a is done, so the
   sheet ID comes from `.env`):
   ```powershell
   python tools/run_local.py --worksheet "image_vo" --rows 2 --dry-run
   ```
   You should see `would process: row 2` and exit 0. If you see prereq errors,
   fix the `.env` and rerun.

---

## Daily use

You can run the script three ways depending on how much you want to type.

### Mode 1 — fully interactive (lazy mode)

```powershell
.\.venv\Scripts\Activate.ps1
python tools/run_local.py
```

Assumes `BULKVID_DEFAULT_SHEET_ID` is set in `.env` (step 5a). The script
lists every tab in the spreadsheet, you pick one (by number or name), it
tells you how many unprocessed rows are in that tab, you press Enter to
process them all (or type a custom range to override), and it runs. Best
when you don't remember which tabs the bulk team has set up today.

```
Available tabs:
  1. image_vo
  2. simple
  3. cartoon
  4. 4Images_VO2
  q. quit

Pick a tab (number or name): 3

6 unprocessed row(s): 3, 5, 7, 9, 11, 14
[Press Enter to process all of them, or enter a row range like 5,7,9-12 to override; 'q' to quit]:
```

### Mode 2 — partial (pick the tab, default to all unprocessed)

```powershell
python tools/run_local.py --worksheet "cartoon"
```

Skips the tab picker. Reads the worksheet, finds every row whose `Ready Video 1`
cell is empty, and processes those. If there are zero unprocessed rows, prints
"Nothing to do" and exits.

### Mode 3 — scripted (everything specified, no prompts)

```powershell
python tools/run_local.py --worksheet "cartoon" --rows 5,7,9-12
```

Runs immediately. If any of those rows already have a video, prints a one-line
`warning: row(s) 5, 7 already have a video — their cells WILL BE OVERWRITTEN`
and proceeds. Use this when you want to deliberately re-run a row, or in CI.

### CLI flags

| Flag | Required | Default | Meaning |
|---|---|---|---|
| `--sheet-id` | **no** | `BULKVID_DEFAULT_SHEET_ID` from `.env` | The long token from the spreadsheet URL. Set the env var once, never type it again. Pass `--sheet-id` only when working against a different sheet. |
| `--worksheet` | **no** | interactive tab picker | Tab name at the bottom of the sheet (case + spaces exact). Omit to get a numbered menu. |
| `--rows` | **no** | all unprocessed rows in the chosen tab | 1-indexed sheet rows. Single (`5`), list (`5,7`), range (`9-12`), or mix (`5,7,9-12`). |
| `--layout` | no | auto-detect from worksheet name + row-1 headers | One of: `image_vo`, `four_images_vo2`, `simple`, `cartoon`. Pass this only when auto-detection picks the wrong layout (rare). |
| `--concurrency` | no | `BULKVID_MAX_CONCURRENT_ROWS` from `.env` (10) | Max rows in flight at once. |
| `--log-file` | no | none | Tee logs into a file. Useful when sharing logs with Yoav. |
| `--dry-run` | no | off | Parse + read the sheet, exit before any vendor calls. |

If you run without a terminal (CI, piped invocation), both `--worksheet` and
the row selection must be explicit — the script refuses to prompt without a
TTY and exits with a clear error.

**Layout auto-detection** mirrors the Apps Script in your Sheet
([`_detectTabType` in apps_script/Code.gs](../apps_script/Code.gs#L88)):

1. Worksheet name contains `x4`  →  `image_vo` (the 4-video generation flow)
2. Worksheet name contains `simple`  →  `simple`
3. Worksheet name contains `cartoon`  →  `cartoon`
4. Else, row 1 header has `How Many`  →  `four_images_vo2`
5. Else, row 1 header has `Manual Image`  →  `image_vo`
6. None of the above  →  error asking you to pass `--layout` explicitly

So in practice you only ever set `--worksheet` and `--rows`. The bulk team's
existing tabs already follow this naming convention because the PA Sheet menu
relies on the same detection.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | All requested rows succeeded. |
| 1 | At least one row failed (others may have succeeded; check the summary). |
| 2 | Prereq / sheet-read / config problem. Nothing was processed. |
| 130 | Ctrl-C. Some rows may have completed and been written to the sheet before the interrupt. |

---

## What you see while it runs

Two streams of output, both on stdout:

1. **JSON log lines** — one per pipeline step. Same format PA writes to its log
   files. Useful for debugging; ignorable during normal runs.
2. **Human summary lines** — one per finished row, plus the final summary:
   ```
   Row 5: SUCCESS · 4 videos · $0.1234 · 87.4s
   Row 7: SUCCESS · 4 videos · $0.1198 · 91.2s
   Row 9: FAILED ARTICLE_FETCH_FAILED · "timeout after 15s"
   Row 10: SUCCESS · 4 videos · $0.1205 · 89.1s
   ...
   Done — 11 succeeded, 1 failed, $1.4012 total, 213.5s elapsed
   ```

Per-run log files also land in `<BULKVID_DATA_DIR>/logs/<job_id>.log` (one file
per invocation). The `<job_id>` is printed at the start of the run — keep it
handy if you need to share the log later.

---

## Troubleshooting

**`error: SHEETS_SERVICE_ACCOUNT_FILE is empty in .env`** — Step 4/5 above.
The `.env` must point to the JSON file, and the file must exist.

**`error: could not read sheet — ... WorksheetNotFound`** — The `--worksheet`
name doesn't match what's in the sheet. Check case + spaces. "Image VO" and
"image_vo" are different.

**`error: could not read sheet — ... PERMISSION_DENIED`** — The sheet isn't
shared with the service-account email. Open the sheet → Share → add the
`client_email` from the SA JSON as Editor.

**`warning: requested rows not found in worksheet '...': 99`** — Row 99
doesn't exist in the sheet, or its required cells (article URL, manual image,
etc.) are empty so the reader skipped it. Check the sheet.

**`Row N: FAILED IMAGE_GEN_FAILED · "..."`** — kie.ai returned an error.
Common causes: rate limit (transient — rerun later), key revoked (ask Yoav),
the prompt tripped a content filter (sensitive vertical). Check the JSON log
line for the row to see the exact upstream error.

**`Row N: FAILED ZAPCAP_FAILED_KEPT_NO_CAPTIONS`** — The video shipped without
burned-in subtitles because ZapCap failed. Not a hard failure; the video URL
is still in the sheet, just without captions. Re-run with `zapcap=No` on that
row if you want to avoid the (small) extra cost.

**Ctrl-C didn't stop instantly** — Asyncio waits for the currently-running
HTTP requests to finish before exiting. Give it 5-10 seconds. Rows that had
ALREADY completed before Ctrl-C are written to the sheet; rows in flight at
Ctrl-C are abandoned (you can rerun for those row numbers).

---

## Staying up to date

When Yoav ships a fix or a new feature, you get it with:

```powershell
git pull
pip install -e ".[dev]"     # only if pyproject.toml changed
```

No script change on your end. The runner imports the shared pipeline modules,
so the same `git pull` that updates PA also updates your local runner.

If Yoav changes admin-tuned prompts or the sensitive-apparel keyword list, ask
him for a fresh `settings.db` — those live in the database file, not in code.
