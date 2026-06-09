# 2026-06-09 — libSQL "no column named 'key'" outage

## Symptom

Starting 2026-06-09 08:17 UTC, every "Generate selected rows" submit produced
a job that:

- appeared in the sidebar archive as `completed 1/1 · $0.0000` within ~1 s
- wrote no `Ready Video` / `Ready Image` URL to the sheet
- left the user with no visible error popup

Affected every tab (cartoon, simple, paste-text-on-img, video-with-avatar,
…) — i.e. the breakage was infrastructure-wide, not pipeline-specific.

## Root cause

`SettingsStore._load_sync` does:

```python
cur = self._conn.execute("SELECT key, value FROM settings")
return {row["key"]: row["value"] for row in cur.fetchall()}
```

In libsql remote mode, the cursor returned by libsql does not expose
`"key"` (a SQL non-reserved keyword) under the name `"key"` in
`cursor.description`. `_DictRow.__getitem__("key")` therefore raises
`IndexError: no column named 'key'`.

The bug has been latent forever. It only fired today because the
`settings` table was empty until 08:16:50 — at which point the avatar
catalog write (`settings_changed key=tiktok_avatar_catalog`) inserted
the first row. From the next `_load_sync` onward, the dict comprehension
hit `row["key"]` and exploded.

## Cascade

One `IndexError` produces two distinct symptoms:

1. **Every row fails fast.** `BatchRunner._handle_row` calls
   `_row_timeout_seconds(tab)` → `store.get(...)` →
   `_ensure_cache` → `_load_sync` → IndexError. Caught by the
   row-level `except Exception` → `STATUS_INTERNAL_ERROR`. Provider
   calls (kie, OpenAI, Rendi, TikTok) are never made. Cost stays
   `$0.0000`. Sidebar shows `completed 1/1`.

2. **The worker process crashes every ~30 s.** `BatchRunner.run`
   calls `_emit_heartbeat(idle=True)` → `_stuck_threshold_seconds`
   → same `store.get()` → same IndexError. **This call is NOT wrapped
   in try/except**, so the exception escapes the runner loop. The
   worker exits 1, supervisord restarts it, cycle repeats. Logs
   show `exited: worker (exit status 1; not expected)` every ~30 s.

## Goals

1. Settings reads work on libsql remote mode (production today).
2. A transient settings_store failure cannot crash the worker.
3. Submitting rows that are all dedup-dropped no longer looks
   indistinguishable from "job succeeded with no output".
4. Regression tests prevent reintroducing any of these.

## Constraints

- Hot prod fix; don't rewrite the libsql wrapper.
- No new dependencies.
- Keep the `_DictRow` API surface stable (rows are accessed by name in
  ~30 places across queue/settings_store).
- No breaking changes to Apps Script payload shape.

## Approach

### Fix 1 — `settings_store.py`: read by index, not by column name

Switch `_load_sync` and `_get_sync` from `row["key"]` / `row["value"]`
to `row[0]` / `row[1]`. These are the only two queries in the entire
codebase that access columns named `key` / `value`. Integer indexing
sidesteps the libsql description quirk entirely.

```python
def _load_sync(self) -> dict[str, str]:
    cur = self._conn.execute("SELECT key, value FROM settings")
    return {row[0]: row[1] for row in cur.fetchall()}

def _get_sync(self, key: str) -> str | None:
    cur = self._conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    )
    row = cur.fetchone()
    return row[0] if row is not None else None
```

Why not switch the SELECT to `SELECT key AS k, value AS v FROM settings`?
Because we don't know whether the libsql description bug is specific to
the `key` identifier or hits any column whose name the wrapper trips on.
Index access is provably robust regardless.

### Fix 2 — `runner.py`: don't let the heartbeat crash the loop

Wrap the `_emit_heartbeat(idle=True)` call in `try/except Exception`,
log a warning with the error, continue the loop. The heartbeat is
observational; missing one tick is acceptable. Crashing the worker is
not.

### Fix 3 — `queue.py` + `routes/jobs.py`: surface dedup-suppressed
submits to Apps Script

Change `JobQueue.enqueue` to return `(job_id, kept_count,
dropped_row_nums)` instead of bare `job_id`. The route returns
`row_count = kept_count` (was `len(rows)`) and adds `dropped_row_nums`
to the `SubmitJobOut`. Apps Script alerts the user when
`row_count == 0` so a stuck-job scenario cannot masquerade as
"job complete, no output."

### Alternatives rejected

- **Quote the column in SQL** (`SELECT "key", "value" FROM settings`).
  Doesn't fix the description bug — libsql wraps tuples it gets from
  the wire and we don't control what names land there. Wouldn't help
  if a future column name hits the same trap.
- **Rename the `key` column to e.g. `setting_key`.** Schema migration
  on a live Turso DB for a column that's a primary key. Risk and blast
  radius too high for a hot fix.
- **Patch `_DictRow` to fall back to integer indexing on missed names.**
  Hides the underlying bug from every caller and would mask a real
  schema mismatch in the future. Targeted fix is preferable.

## Security

No security surface change. Settings reads run as the worker process
under its existing Turso auth token. No new ingress, no new data
exposure. The dedup-surfacing change makes a previously silent
behaviour visible — strictly more transparent to operators.

## Observability

- `runner_heartbeat_failed` warning log on the new wrapped exception
  path (namespace `bulkvid runner`), so a recurring settings_store
  issue shows up in HF logs.
- Existing `settings_store_load_retry` log already covers the
  underlying read failure path.
- Dedup-suppression already logs nothing today; the route layer will
  add `job_submit_dropped_rows` info log when `dropped_row_nums` is
  non-empty.

## Testing (rule 18)

New unit tests:

1. `test_load_sync_works_when_column_name_lookup_unavailable` —
   wrap a sqlite3 cursor so `description` returns `()`/`None`,
   confirm `_load_sync` still returns the right dict via integer
   access. Mirrors the libsql shape.
2. `test_get_sync_works_when_column_name_lookup_unavailable` —
   same pattern for the single-column read path.
3. `test_runner_survives_heartbeat_settings_store_error` — inject a
   `SettingsStore` whose `get()` raises, run one idle tick, confirm
   the loop did not exit and a warning was logged.
4. `test_submit_returns_dropped_rows_when_dedup_kept_nothing` —
   pre-seed a queued job with the same `(sheet, worksheet, row_num)`,
   submit again, assert `row_count == 0` and `dropped_row_nums`
   contains the row that was dropped.

Run the full unit suite green before claiming done.

## Settings audit (rule 15)

No new user-facing knobs. The dedup-surfacing change is a UX
improvement; no operator config needed.

## Recovery (no manual data step required)

The settings table already contains the `tiktok_avatar_catalog` row
that triggered the bug. Once the deploy lands, `_load_sync` reads it
correctly and rows start processing again. No manual cleanup needed.
`active_jobs=0` in the latest logs, so no stuck jobs to kill.

## Open questions

- Long-term: file an upstream issue with the libsql Python driver?
  Worth a one-line repro script demonstrating the description-name
  loss for the column literally named `key`. Out of scope for this PR.
