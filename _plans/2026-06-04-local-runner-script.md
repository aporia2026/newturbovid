# Local runner script — bypass PA for one specific user

Date: 2026-06-04
Status: proposed (awaiting approval)
Owner: Yoav

## Goals

1. Give ONE specific bulk-team user the ability to process Sheet rows on his own
   machine without going through PythonAnywhere. PA has been flaky enough lately
   that this user wants an offline path.
2. Achieve this with **zero fork** of the pipeline code. The script must be a
   thin entry point on top of the existing `src/bulkvid/` modules. Fixes you ship
   to PA must automatically reach the local user on `git pull` — no duplicated
   logic, no risk of drift.
3. Keep production parity: the same sensitive-apparel safeguard, same admin-tuned
   prompts (loaded from his local `settings.db`), same script-gen behaviour, same
   storage / vendor calls. The local user is doing real work, not a dev sandbox.

## Non-goals

- No public URL, no tunnel, no firewall holes. Backend never binds to anything
  other than process-local memory.
- No FastAPI HTTP server, no SQLite job queue, no worker daemon. He runs the
  script when he wants to process rows; otherwise nothing is running on his
  machine.
- No Apps Script changes. PA users keep the existing Sheet menu / sidebar
  unchanged. This is purely additive.
- Not a long-running watcher. One CLI invocation = one batch of rows. Done.
- No admin panel locally. The script does not edit settings; it just reads them.

## Constraints

- Windows machine. PowerShell first-class.
- Python 3.12 (already required by the rest of the repo via `pyproject.toml`).
- The user is comfortable with a terminal but not a developer — clear errors,
  no traceback walls on common misconfigurations.
- All third-party API calls hit the SAME accounts PA uses (kie, OpenAI, Vertex,
  Rendi, ZapCap, GCS, S3, Tavily, ScrapingBee). No new spend pattern.

## Why this design (Rule 4: alternatives & recommendation)

Three architectures were on the table:

### A — Local backend + public tunnel (Cloudflare / ngrok)
Run `uvicorn` + `worker.py` locally; expose port 8788 via a tunnel daemon; paste
the public URL into the Sheet's BACKEND_URL.
- ➕ Sheet menu UX preserved 1:1.
- ➖ Still publicly reachable (we wanted to escape that, not move it).
- ➖ Tunnel daemon + worker daemon + uvicorn — three processes the user has to
  keep alive.
- ➖ Free tunnel URLs change per session; stable URLs cost.

### B — Browser-side sidebar fetch (no tunnel, refactor Apps Script)
Move `UrlFetchApp` calls in `Code.gs` into `Sidebar.html` JavaScript so requests
originate from the user's browser (which CAN reach localhost).
- ➕ Zero public exposure. Free.
- ➖ Real refactor of `Code.gs` + `Sidebar.html`. Backend needs CORS. New auth
  shuttling logic.
- ➖ Future-fragile (depends on Google's iframe sandbox policy on localhost).
- ➖ Engineering cost not justified for one user.

### C — Standalone CLI script (RECOMMENDED, chosen)
A single `tools/run_local.py`. CLI args specify sheet + tab + row range. Reads
rows directly via the existing `SheetsClient`, dispatches to the existing
`process_*_row` functions with the existing `PipelineClients` bundle, writes
results back immediately via the same `SheetsClient.batch_write_video_urls`.
- ➕ Zero infra. Nothing listens. Nothing hosts. Pure invocation.
- ➕ ~150 lines new code. ~95% reuse of existing tested pipeline.
- ➕ Logs are stdout — when something breaks, the user copies the terminal.
- ➖ User loses Sheet menu UX (no "Generate Selected Rows" right-click). He
   types `python tools/run_local.py --worksheet "image_vo" --layout image_vo --rows 5,7,9-12` instead.
   He accepted this trade in the planning conversation.
- ➖ One-and-done invocations only; no live sidebar progress. The terminal IS
   the sidebar.

## The maintenance contract (Rule 2: no drift)

| Layer | Local user gets fixes by | Lives in |
|---|---|---|
| `src/bulkvid/pipeline/*` (script gen, image, safety, language, open_comments, cartoon_prompt) | `git pull` + rerun | Shared |
| `src/bulkvid/adapters/*` (kie, sheets, Rendi, ZapCap, OpenAI, gemini_tts, storage, article_fetch, atlascloud) | `git pull` + rerun | Shared |
| `src/bulkvid/orchestrator/row_processor_*` | `git pull` + rerun | Shared |
| `src/bulkvid/orchestrator/settings_store.py` + `runtime_settings.py` | `git pull` + rerun | Shared |
| `src/bulkvid/config.py`, `src/bulkvid/logging.py`, `src/bulkvid/models/row.py` | `git pull` + rerun | Shared |
| `src/bulkvid/worker.py::build_pipeline_clients` | `git pull` + rerun (the local script imports this builder) | Shared |
| `src/bulkvid/routes/*`, `src/bulkvid/main.py`, `src/bulkvid/auth.py` | Not used locally | PA-only |
| `src/bulkvid/orchestrator/queue.py`, `runner.py`, `sheet_writer.py` (the buffer loop) | Not used locally | PA-only |
| Apps Script | Not used locally | PA-only |
| `tools/run_local.py` (new) | `git pull` + rerun | Local-only |

**No pipeline logic lives in `tools/run_local.py`.** Everything it does is
either argparse, importing existing modules, or wiring an asyncio loop.

## Design

### Files added (new)
- `tools/run_local.py` — the script. ~200 lines.
- `tools/README.md` — first-run guide for the local user (install, .env,
  service-account JSON, sample commands, troubleshooting).
- `tests/unit/test_run_local.py` — unit tests for the row-range parser and the
  tab→reader/dispatch wiring (with stubbed processors).

### Files modified
- `src/bulkvid/adapters/sheets.py` — three small public methods added,
  purely additive (existing readers unchanged):
  - ``SheetsClient.read_header_row`` — reads row 1 for header-based layout
    detection when the worksheet name carries no layout signal.
  - ``SheetsClient.list_worksheets`` — returns every tab name in the
    spreadsheet, in tab order, for the interactive tab picker.
  - ``SheetsClient.read_processed_row_nums`` — returns the set of 1-indexed
    sheet rows whose ``Ready Video 1`` cell is non-empty. Drives both the
    "all unprocessed" default and the overwrite warning when ``--rows`` is
    explicit.
- `src/bulkvid/config.py` — one new field, ``BULKVID_DEFAULT_SHEET_ID``
  (empty default). Read by ``tools/run_local.py`` when ``--sheet-id`` is
  omitted so the local user doesn't retype the sheet ID every invocation.
  Not used by PA (Apps Script passes the sheet ID per request).
- `.env.example` — documents ``BULKVID_DEFAULT_SHEET_ID``.

### Files NOT touched (explicit)
- `apps_script/*` — Sheet menu unchanged for PA users.
- `src/bulkvid/*` — every existing module is consumed as-is.
- `pyproject.toml` — no new dependencies. The script uses only what the worker
  already imports.

### Public CLI surface
```
python tools/run_local.py \
  [--sheet-id <google sheet id>] # omit -> settings.BULKVID_DEFAULT_SHEET_ID from .env
  [--worksheet "<tab name>"]     # omit -> interactive tab picker
  [--rows <range spec>]          # omit -> all unprocessed in the chosen tab
  [--layout {image_vo|four_images_vo2|simple|cartoon}]   # optional override
  [--concurrency N]              # default = settings.BULKVID_MAX_CONCURRENT_ROWS
  [--log-file PATH]              # optional tee to file
  [--dry-run]                    # parse + validate, don't call any vendor
```

### Three invocation modes (lazy-user first, Rule 10)

1. **Fully interactive** — just `--sheet-id`. The script lists every tab in
   the spreadsheet via ``SheetsClient.list_worksheets``, the user picks one
   by number or name, the script reports how many unprocessed rows are in
   that tab (computed via ``read_processed_row_nums``), and prompts: press
   Enter to take them all, or enter a row range to override. Best for the
   bulk-team user who doesn't remember today's tab names.
2. **Partial** — ``--sheet-id`` + ``--worksheet``. Skips the picker. No row
   prompt either — defaults straight to processing all unprocessed rows in
   that worksheet. If zero are unprocessed, prints "Nothing to do" and exits
   0. Best for "I know which tab; do everything pending."
3. **Scripted** — every flag specified. No prompts. If any ``--rows`` value
   already has a video, prints a one-line OVERWRITE warning and proceeds (the
   user was explicit, so we trust them; matches the policy chosen in the
   planning conversation). Best for CI / cron / deliberate re-runs.

If stdin is not a TTY and a required value is missing, the script exits 2
with a clear "this needs a terminal" message instead of hanging on
``input()``.

The runner **auto-detects the row layout** from the worksheet's name and (as
a fallback) row-1 headers, mirroring ``_detectTabType`` in
``apps_script/Code.gs``:

1. Worksheet name contains ``x4`` → ``image_vo``
2. Worksheet name contains ``simple`` → ``simple``
3. Worksheet name contains ``cartoon`` → ``cartoon``
4. Else row 1 has ``How Many`` header → ``four_images_vo2``
5. Else row 1 has ``Manual Image`` header → ``image_vo``
6. None of the above → error; the user re-runs with ``--layout``.

``--layout`` is an optional override for the unusual case where auto-detection
picks the wrong layout. Values mirror the existing ``TAB_*`` constants in
``src/bulkvid/orchestrator/queue.py`` so the internal vocabulary stays
consistent with the rest of the codebase.

### Row-range parser
- Accepts comma-separated tokens: `5`, `5,7`, `5-9`, `5,7,9-12`.
- 1-indexed sheet rows (the user thinks in sheet rows, never in array offsets).
- Rejects `0`, negatives, `9-5` (reversed), non-numeric tokens with a clear
  error message naming the bad token.
- Returns a sorted, deduped `list[int]`.

### Execution flow
```
1.  parse argv → ParsedArgs(sheet_id, tab, row_nums, concurrency, log_file, dry_run)
2.  configure_logging() + open optional --log-file tee
3.  get_settings() (reads .env)
4.  Validate prerequisites:
       - settings.SHEETS_SERVICE_ACCOUNT_FILE non-empty AND file exists
       - settings.OPENAI_API_KEY, KIE_AI_KEYS, RENDI_API_KEY non-empty
       - any tab-specific requirement (none extra for now)
    Each failure prints ONE actionable line and exits 2.
5.  Build SheetsClient (gspread + service account)
6.  Build SettingsStore at settings.BULKVID_DATA_DIR / "settings.db" + run
       the same legacy-key migration the worker runs
7.  Build PipelineClients via worker.build_pipeline_clients(settings); attach
       settings_store
8.  Read rows from the sheet for the chosen tab:
       - image_vo  → SheetsClient.read_image_vo_rows
       - simple    → SheetsClient.read_image_vo_rows, convert ImageVORow→SimpleRow
                     (identical fields, different type used for dispatch)
       - four_images_vo2 → SheetsClient.read_four_images_rows
       - cartoon   → SheetsClient.read_cartoon_rows
9.  Filter to rows whose row_num is in row_nums. Log + print which requested
       row_nums were NOT found in the sheet (typo guard).
10. If --dry-run: print the parsed plan, exit 0.
11. asyncio.Semaphore(concurrency) — gather all per-row tasks.
12. Per row: call process_<tab>_row(row, clients, job_id=local_job_id).
       On completion (success or failure), immediately call
       sheets.batch_write_video_urls([PendingWrite(...)]) so the user sees
       the cell populate live in the Sheet. Print one summary line:
       `Row 7: SUCCESS / 4 videos · $0.1234 · 87.4s`
       `Row 8: FAILED article_fetch_failed · "timeout after 15s"`
13. After gather: print final summary (N succeeded, M failed, total cost,
       elapsed). Exit 0 if all succeeded, 1 otherwise.
14. KeyboardInterrupt: cancel pending tasks, await in-flight to finish so
       partial results land in the Sheet, print summary, exit 130.
```

### local_job_id format
`local-<host_hostname>-<utc_ts>`, e.g. `local-yoav-pc-20260604-153012`.
Goes into the structlog context so logs are filterable; also embedded in any
GCS / S3 keys the storage adapter generates, mirroring how PA uses the queue
job_id.

## Security (Rule 13)

### What's sensitive
- The `.env` file (all API keys + Google service-account credential material).
- The `SHEETS_SERVICE_ACCOUNT_FILE` JSON (private key for the service account
  with write access to the bulk team's sheets).
- Any local cached state in `BULKVID_DATA_DIR` (`settings.db` — admin-edited
  prompts and keyword lists; non-secret but bulk-team-confidential).

### Delivery
- `.env` and the service-account JSON are handed to the local user
  **out-of-band** (password manager share, encrypted email, or USB). They are
  NEVER committed and NEVER pasted into chat / Slack / Apps Script properties.
- `.gitignore` already excludes `.env`, `data/`, and `*.json` at the root for
  service-account files. We will verify this still holds before merging.

### Surface area on the local machine
- No port bound. The script runs and exits; nothing listens.
- No auth required on the script itself — the security boundary is "you have
  the .env file." That matches reality: anyone who has the .env can call the
  same APIs directly via curl. The script adds nothing new to that boundary.
- Logs (stdout + optional file) MUST NOT print the contents of any API key or
  service-account field. The existing `bulkvid.logging` configuration already
  redacts via structlog; we add nothing that bypasses it.

### What we don't log
- API keys, service-account JSON contents, full request bodies that contain
  credentials. (Already enforced by the existing adapter logging.)
- Full article body text (already truncated by `script_gen` at 1500 chars).

### Failure modes that must be safe
- Service-account file missing → exit 2 with a clear pointer to `tools/README.md`.
- Sheet not shared with the SA → gspread raises `WorksheetNotFound` /
  `APIError` → caught at the read step → print "Sheet not shared with service
  account `<email>` — share it with that email and retry." Exit 2.
- Vendor key missing for a path the row needs (e.g. ZapCap=Yes but no
  `ZAPCAP_API_KEY`) → the existing row processor returns
  `STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS` and the video still ships. Same as
  PA behavior; no change.

## Observability (Rule 14)

### Namespace
All new logs use `[run_local <step>]` via `get_logger("run_local")`. Existing
modules keep their own namespaces (`[row …]`, `[sheets …]`, `[kie …]`, etc.) so
the local user sees the SAME structured log stream PA produces, just on his
terminal.

### Logged at each step (with values, not just events — Rule 14 enforces values)
- `run_local_start` — argv-derived plan: `tab`, `requested_row_count`,
  `concurrency`, `sheet_id`, `dry_run`.
- `run_local_settings_loaded` — which keys are configured (booleans only:
  `kie_keys_configured=3`, `zapcap_configured=True`, etc.).
- `run_local_sheet_read` — `tab`, `rows_found_in_sheet`, `rows_matched_request`,
  `rows_requested_not_in_sheet=[...]`.
- `run_local_row_dispatch` — `row_num`, `tab`, `local_job_id`.
- `run_local_row_done` — `row_num`, `status`, `videos`, `cost_usd`, `elapsed_s`.
- `run_local_writeback` — `row_num`, `cells_written`. (Or failure with error.)
- `run_local_shutdown` — `succeeded`, `failed`, `total_cost_usd`, `elapsed_s`,
  `interrupted` (True/False).

### Where logs go
- stdout, always (structlog → human-readable in dev, JSON in prod — same as
  the worker; controlled by `BULKVID_ENV`).
- `--log-file PATH` tees a copy. Useful when the user wants to share a log
  with you for debugging without having to scroll their terminal.

## Testing (Rule 18 — no exception)

### Unit tests added — `tests/unit/test_run_local.py`
1. `parse_row_range` — golden cases: single, comma list, range, mixed. Edge
   cases: dedup, sort, reversed range (`9-5`), zero, negative, non-numeric,
   empty string. Each failure path asserts the bad token appears in the error.
2. `_resolve_reader` — given each `--layout` value, returns the matching read
   function and dispatch function. Unknown tab raises `ValueError`.
3. `_pending_write_from_result` — builds a PendingWrite with the right
   `tab_type`, `row_num`, `video_urls`, `status`, `error`. Image-VO with 4
   video_urls; cartoon with 2; simple with 1; four_images with `how_many` urls.
4. `run_batch` (the async loop) with stubbed row processors and a fake
   SheetsClient:
   - Dispatches each row to the right processor based on tab.
   - Caps concurrency at `--concurrency`.
   - Writes back per row immediately (one `batch_write_video_urls` call per
     finished row).
   - On a row exception, records `STATUS_INTERNAL_ERROR` and continues — the
     loop survives.
   - KeyboardInterrupt cancels pending, awaits in-flight, returns partial
     results.

### Why these tests and not more
- The row processors themselves are already covered by
  `tests/unit/test_row_processor_*.py` and the runner orchestration is
  covered by `tests/unit/test_runner.py`. We do NOT re-test those. We test
  ONLY what is new: argv parsing, tab dispatch, and the local concurrency
  loop.
- No integration test against a real Google Sheet — that would require a
  live service account in CI, which we deliberately don't have. Manual
  smoke from the local user is the integration test (documented in
  `tools/README.md`).

### Test command
```
pytest tests/unit/test_run_local.py -v
pytest          # full suite must remain green
```

## Settings audit (Rule 15)

Does the local script introduce any user-tunable behavior that should live in
the settings layer?

- `--concurrency` — already configurable via `BULKVID_MAX_CONCURRENT_ROWS` in
  `.env`. The CLI flag is a per-invocation override; the default is the env
  value. No new settings key.
- `--log-file` — purely operational, not a long-lived preference. No setting.
- Tab choice — per-invocation. No setting.

**Nothing new lands in the settings layer.** All admin-tunable behavior
(prompts, sensitive-apparel keywords) is already in `settings.db` and the
local user gets the SAME admin-edited values you tune on PA's admin panel —
because the local script reads from the SAME `settings.db` path pattern, and
the local user's `settings.db` is populated by either (a) you syncing it from
PA on first delivery, or (b) the registry defaults from
`runtime_settings.registry_defaults()`.

NOTE: option (b) means the local user starts with the BUILT-IN defaults, not
your PA-tuned prompts. If you've customised any prompt on PA, you have to
hand the local user a copy of `settings.db` once for parity. We document
this in `tools/README.md`.

## Risks & open questions

1. **Settings drift between PA and local.** If you tweak the script-gen
   prompt on PA tomorrow, the local user's `settings.db` still has the old
   value. Mitigation: document the sync. Long-term: add an admin endpoint
   that exports `settings.db` so the local user can curl-update on demand.
   Out of scope for this plan.
2. **Per-key kie rate limits shared with PA.** Both PA and the local user
   are now drawing from the same `KIE_AI_KEYS` pool concurrently. If both
   run heavy batches at the same time, kie rate limits trip earlier. The
   adapter already handles rate-limit cooldown; just flagging the
   inter-process visibility gap.
3. **Windows asyncio signal handling.** The worker comment notes
   `add_signal_handler` raises `NotImplementedError` on Windows. The local
   script must accept `KeyboardInterrupt` directly (the script's primary
   target OS is Windows). Covered by the design in step 14.

## Rollout

1. Land this plan + code + tests on `main` after a green run.
2. You hand the local user:
   - The repo URL.
   - The `.env` (out-of-band).
   - The Google service-account JSON (out-of-band).
   - Optionally, a copy of PA's `settings.db` (out-of-band) for prompt parity.
   - A link to `tools/README.md`.
3. He runs through the README first-run section once.
4. First real batch — small (1–2 rows) — to confirm the Sheet writeback works
   end to end. After that, normal use.
