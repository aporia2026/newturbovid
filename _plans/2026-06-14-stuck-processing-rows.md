# Stuck PROCESSING rows + unresponsive kill button — fix plan

Date: 2026-06-14
Owner: Yoav
Status: **In progress** — implementing A + B.

## Symptom

Operator sees rows in the sidebar with growing elapsed timer ("22:05 / Starting..") even though the matching sheet rows already have storage.googleapis URLs filled in. Clicking "Kill job" appears to do nothing — the sidebar keeps showing "Starting..".

Screenshot reference: operator on the `simple` tab at 2026-06-14, ~25 rows landed with URLs in the sheet, but a subset stayed "Starting" for 17–22 min. Kill button toast did not move them.

## Root cause

Two independent bugs compound:

### Bug 1 — `record_result` failures lose the result silently

[`BatchRunner._handle_row`](../src/bulkvid/orchestrator/runner.py) does, in order:

1. Processor runs → `RowResult`.
2. `await asyncio.wait_for(queue.record_result(...), timeout=30s)` — writes `row_queue.status='done'/'failed'` to the DB.
3. `await asyncio.wait_for(queue.get_job(...), ...)` — looks up sheet routing.
4. `await self._write_back(write)` — submits a `PendingWrite` to the in-memory `CoalescedSheetWriter` buffer; that writer flushes to the sheet every 5 s.

If step 2 raises `TimeoutError` or any other exception, it is **only logged** as `runner_record_result_timeout` / `runner_record_result_failed`. The runner does NOT retry. It moves on to step 4 anyway, the sheet gets the URLs, but `row_queue.status` is stuck at `processing` forever — until the worker restarts and `recover_orphaned_rows` resets it back to PENDING (which then re-runs the row, re-spending the cost).

This is the root cause of "the sheet says done, the sidebar says Starting." It is also why a worker restart is the user's only workaround — and the restart re-spends every stuck row.

### Bug 2 — Kill doesn't clean up rows and the kill route has no timeout

[`_kill_job_sync`](../src/bulkvid/orchestrator/queue.py) only updates `jobs.status`. It does NOT touch `row_queue`. After kill, the job moves to KILLED (sidebar archives it), which hides the row breakdown — but only IF the kill request reaches the DB.

The route layer's `queue.kill_job(...)` is `async with self._lock: await asyncio.to_thread(...)` with **no `asyncio.wait_for` bound**, unlike the worker's queries which got 30 s hard timeouts in commit `62c0950`. When Turso flaps, the kill POST hangs at the lock or in the thread, eventually 500s at the HTTP layer, and the user sees the "Could not kill" toast. From the user's seat: "doesn't let killing this process."

Additionally, even when kill DOES land, in-flight `_handle_row` tasks keep running for their full timeout budget before being recorded as FAILED — the operator wants "stop now," not "stop in 12 minutes."

## Fixes (A + B)

### A — Background `record_result` retry queue in `BatchRunner`

When `record_result` raises (TimeoutError or any other exception), instead of dropping the result on the floor:

1. Push `(queued_id, result)` onto an in-process `asyncio.Queue` named `_pending_records`.
2. A long-running background task `_drain_pending_records` (started in `BatchRunner.run` alongside the main claim loop) drains this queue, retrying each entry with exponential backoff (1 s → 2 s → 4 s → 8 s, capped at 30 s, retry forever until success or shutdown).
3. Successful retries log `runner_record_result_recovered` with `queued_id` and `attempts`.
4. On shutdown, drain the pending queue with a finite budget (e.g. 30 s total). Any leftover entries get logged as `runner_pending_record_dropped` with the full result payload so an operator can manually patch the row if needed.

The main `_handle_row` path stays non-blocking: `record_result` is tried once with the existing 30 s timeout; on failure the entry goes to the retry queue and the path continues to `_write_back`. The sheet still gets the URLs immediately.

Buffer size: the queue is unbounded in memory, but bounded in practice because `_handle_row` only enqueues on `record_result` failure (rare in normal operation). If the buffer grows past 1000 entries, log a `runner_pending_records_backlog` warning every 100 entries — that's the operator's signal that Turso is in a bad state.

Idempotency: `_record_result_sync` is already idempotent — it does an UPDATE on a specific `id` with a deterministic status. Retrying is always safe.

### B — Kill cleans up rows + bounded kill route

Two changes to the queue:

1. `_kill_job_sync(job_id)` runs the existing `UPDATE jobs SET status='killed'` AND a new `UPDATE row_queue SET status='failed', finished_at=?, result=? WHERE job_id=? AND status IN ('pending','processing')`. The result JSON carries `{"status":"KILLED","error":"killed by user","row_num":<n>}` so the sidebar's error renderer shows "killed by user" instead of a blank state. Returns `(jobs_killed, rows_aborted)` — sidebar surfaces "Killed N rows" in the success toast.
2. Same change to `_kill_all_sync` — touches every PROCESSING/PENDING row for matching jobs.

Two changes to the route layer:

1. Wrap `queue.kill_job(...)` / `queue.kill_all_jobs(...)` in `asyncio.wait_for(timeout=10s)`. A hung kill returns HTTP 504 with `{"error":"kill timed out — worker may be hung; restart the backend"}`. The operator sees a real diagnostic, not silence.
2. The 10 s is loose enough to absorb a slow libsql roundtrip (cold-start ~5 s) but tight enough that the sidebar's 30 s Apps Script `UrlFetch` cap doesn't trip.

In-flight `_handle_row` tasks for killed rows still run to completion in the worker — but when they DO finish, `record_result` will find the row already in `failed` state and the UPDATE is a no-op (the `_record_result_sync` UPDATE doesn't filter by current status, so the killed row would be overwritten to `done` if the processor succeeded). To avoid that overwrite race, the UPDATE in `_record_result_sync` gets a `WHERE status NOT IN ('failed')` clause so a killed row stays killed. (Worker-side concession: a processor that succeeded mid-kill loses its result, but the user explicitly asked for kill, so this is correct.)

## Observability (CLAUDE.md rule 14)

New log lines (namespace `[runner …]` to match existing style):

- `runner_record_result_failed_buffered` — record_result raised; pushed to retry queue. `queued_id`, `queue_size_after`, `error_type`, `error[:200]`.
- `runner_record_result_recovered` — retry succeeded. `queued_id`, `attempts`, `total_wait_s`.
- `runner_pending_records_backlog` — buffer over 1000 with 100-entry granularity. `pending`.
- `runner_pending_record_dropped` — on shutdown drain timeout. `queued_id`, `result` (the full RowResult).
- `runner_pending_drainer_start` / `runner_pending_drainer_stop` — lifecycle.

New log lines on the kill path:

- `job_kill_rows_aborted` — INFO with `job_id`, `rows_aborted`, `by`.
- `job_kill_timeout` — WARNING with `job_id`, `timeout_s`, `by`. The 504 response carries the same message.

## Settings (CLAUDE.md rule 15)

Add to the runtime settings store (admin-editable, env-overridable):

- `record_result_retry_max_seconds` (default 300 s) — soft cap on per-entry retry budget; entries beyond this get logged as `runner_pending_record_giveup` and dropped. Default 5 min covers a typical Turso flap; admin can raise to 30 min for a known multi-hour outage. Env: `BULKVID_RECORD_RESULT_RETRY_MAX_SECONDS`.
- `kill_call_timeout_seconds` (default 10 s) — the route-layer `wait_for` bound. Env: `BULKVID_KILL_CALL_TIMEOUT_SECONDS`.

Both surface in the existing admin runtime-settings panel under a new "Resilience" subsection. The settings page already has subsection grouping — slot these between "Row timeouts" and "Stuck-row threshold."

## Testing (CLAUDE.md rule 18)

New unit tests in `tests/unit/test_runner.py`:

- `test_record_result_failure_buffers_and_recovers` — monkeypatch `queue.record_result` to fail twice then succeed. Run one row through the runner. Assert: row eventually lands in DONE state, `runner_record_result_recovered` logged with `attempts >= 2`, `_write_back` still called immediately (sheet not blocked on DB recovery).
- `test_record_result_persistent_failure_logged_on_shutdown` — `queue.record_result` raises forever. Run one row, request shutdown, drain. Assert: `runner_pending_record_dropped` logged with the result payload; row still in PROCESSING in DB (operator must recover manually but the result is in logs).
- `test_pending_records_drainer_does_not_block_main_loop` — concurrency proof: while one record_result hangs in retry, claim_next_row keeps draining new rows.

New unit tests in `tests/unit/test_queue.py`:

- `test_kill_job_aborts_processing_and_pending_rows` — enqueue 3 rows, claim 2 (PROCESSING), leave 1 PENDING. Kill. Assert: job KILLED, all 3 rows FAILED, result JSON carries `"killed by user"`.
- `test_kill_all_jobs_aborts_rows_across_jobs` — same shape, two jobs.
- `test_kill_job_returns_rows_aborted_count` — return shape changes from `bool` → `tuple[bool, int]`; existing callers updated.
- `test_record_result_does_not_overwrite_killed_row` — kill a row, then call record_result with status=SUCCESS, assert row stays FAILED.

New unit tests in `tests/unit/test_routes_jobs.py`:

- `test_kill_job_returns_504_on_timeout` — patch `queue.kill_job` to hang, route returns 504 with diagnostic body.
- `test_kill_all_jobs_returns_504_on_timeout` — same for kill-all.

Existing tests to update:

- `test_kill_job_works_for_queued_and_running` — `kill_job` now returns `(True, rows_aborted)` instead of `True`.
- `test_kill_job_noop_for_completed` — `(False, 0)`.
- `test_killed_job_pending_rows_are_not_claimed` — pending rows are now FAILED (not PENDING-but-blocked), so `claim_next_row` returns None for a different reason. Adjust assertion.

## Security (CLAUDE.md rule 13)

Same auth model as today — the kill endpoints are already bearer-authed. The retry queue is in-process memory only (no new attack surface). The new logging deliberately excludes user payloads — only the `RowResult` (status, video_urls, error string) — same surface area the existing `runner_record_result_failed` already had.

## Out of scope (V1)

- **Periodic stuck-row sweeper** (Option C from the analysis): a 60 s background sweep that finds DB rows in PROCESSING longer than `2 × row_timeout` AND not in `_in_flight`, marking them FAILED. Adds complexity for a third-layer safety net; the A+B retry + clean-kill combination handles the realistic failure modes. Ship if A+B prove insufficient.
- **Persistent retry queue** (across worker restarts). The in-process queue dies with the worker. If `runner_pending_record_dropped` fires in practice, we'll add a `pending_records` SQLite table.
- **Mid-flight `_handle_row` cancellation on kill**. Killing currently lets the in-flight row finish its processor — bills the cost but the result is discarded. A clean cancellation would require plumbing the kill signal into the runner's `_in_flight` task map and `task.cancel()`-ing each. Punted to a follow-up because the operator's primary complaint is the sidebar, not the cost. Add later if billing complaints arrive.

## Alternatives considered (CLAUDE.md rule 4)

1. **Just B (kill cleans up + bounded)** — fastest ship, fixes the immediate "can't stop it" pain. But leaves rows stuck PROCESSING and re-spending on worker restart. Rejected because the root-cause fix is small.
2. **In-thread retry (block `_handle_row` until record_result succeeds)** — simpler than the background queue, but holds the semaphore slot indefinitely. With `max_concurrent=10`, ten simultaneous Turso flaps deadlock the runner. Rejected.
3. **Periodic sweeper alone (Option C)** — works as a safety net but doesn't fix the underlying loss. A row would stay "Starting" for `2 × row_timeout` (~24-40 min) before being marked FAILED. The retry queue settles in seconds. Rejected.

## Acceptance

- Operator manually verifies on HF Spaces after deploy: trigger a batch, kill mid-flight, all rows immediately move to "killed by user" in the sidebar.
- Force a record_result failure (env var `BULKVID_RECORD_RESULT_FORCE_FAIL=1` — tiny test hook, removed after ship) and watch the retry queue drain.
- `pytest tests/unit/test_runner.py tests/unit/test_queue.py tests/unit/test_routes_jobs.py -q` green.
