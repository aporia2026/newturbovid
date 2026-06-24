# Fast-fail kill + poll-route DB timeouts

Date: 2026-06-14
Owner: Yoav
Status: **In progress**

## Symptoms

Operator on HF Spaces today, ~14:23 UTC: sidebar shows banner *"Worker not claiming for 39m"* on the `paste text on img` tab; 0/20 in flight, 48 queued. Clicking **Kill job** does nothing and the active panel later flashes to "Loading…" forever; user reports "I can't close or kill, it's just loading forever."

The two failures are independent of `_plans/2026-06-14-stuck-processing-rows.md` (which fixed kill + record_result loss inside the runner). The fixes shipped in d94b01b are correct but expose two adjacent gaps:

## Root cause

### Gap 1 — Apps Script retries `kill` on 504

[`_fetchJson`](../apps_script/Code.gs) defaults to `maxAttempts=3` with backoffs `[600, 1200]` ms. A 504 from the kill route falls into the 5xx retry branch, so a single hung kill produces THREE upstream POSTs separated by backoff:

```
attempt 1: UrlFetch (up to ~25 s) → 504
sleep 600 ms
attempt 2: UrlFetch (up to ~25 s) → 504
sleep 1200 ms
attempt 3: UrlFetch (up to ~25 s) → 504
```

Worst case the user waits ~75 s before the "Could not kill" toast appears. From the operator's seat that is indistinguishable from "the button is broken." The server already says "I tried for 10 s and gave up — restart me"; retrying that twice more without restarting cannot succeed.

### Gap 2 — `/jobs/poll` has no per-call DB timeout

[`poll_all`](../src/bulkvid/routes/jobs.py) does five libsql roundtrips per cycle:

1. `queue.list_jobs(...)`
2. `queue.list_rows(job_id)` (once per RUNNING job)
3. `queue.eta_medians()`
4. `queue.user_queue_depth(user_email=...)`
5. `queue.user_oldest_pending_row_age_seconds(user_email=...)` (only when stuck)

Calls 3–5 already have `try/except` that drops the field on failure. Calls 1 and 2 do **not**, and none of the five have an `asyncio.wait_for` bound. If Turso flaps and a single roundtrip hangs, the poll hangs forever — the sidebar's initial "Loading…" stays on screen until the user reloads, and the user mistakenly attributes it to the kill button.

The kill route, the runner's `claim_next_row`, and the runner's `record_result` all already have hard libsql timeouts. The poll route is the only sidebar-facing endpoint without one.

## Fix

### A — Apps Script: fast-fail kill (no retry on 504)

In [`killJob`](../apps_script/Code.gs) and [`killAllJobs`](../apps_script/Code.gs), pass `retryOpts: { maxAttempts: 1 }` to `_fetchJson`. The semantics:

- A 504 from the kill route already means "the backend tried for 10 s and the libsql roundtrip is stalled." Retrying without operator intervention (restart) cannot help. Better to surface fast and let the operator decide.
- A 5xx from a transient cold-start would still be retriable on the *next* user click; we don't lose anything by giving up after one attempt.
- A 4xx (auth/not-found) is already `permanent: true` and never retried — unchanged.

Result: a hung kill produces the "Could not kill" toast in ≤10 s instead of ≤75 s. The operator can click again, switch to Stop all jobs, or restart the backend without waiting.

### B — Backend: hard timeout around `/jobs/poll` libsql calls

A new module-level constant in [`routes/jobs.py`](../src/bulkvid/routes/jobs.py):

```python
_POLL_DB_CALL_TIMEOUT_SECONDS = float(
    os.environ.get("BULKVID_POLL_DB_CALL_TIMEOUT_SECONDS") or 15.0
)
```

Default 15 s — loose enough to absorb a worst-case cold-start (~5 s) plus a couple of libsql roundtrips, tight enough that the Apps Script `UrlFetch` 30 s cap never trips. Each call in the poll handler gets wrapped:

- `list_jobs` — **mandatory**. On timeout, raise HTTP 504 with `kill timed out…`-style diagnostic. Without `jobs`, the sidebar has nothing to render. The 504 lets the existing `onFail` handler render `_lastJobs || []` with the "Reconnecting…" banner — exactly what we want.
- `list_rows` (per RUNNING job) — **best effort**. On timeout, drop the row breakdown for that job (`rows_by_job` entry omitted). The sidebar already handles a missing entry — the active card collapses the row list rather than 500'ing the whole poll.
- `eta_medians` — already in `try/except`. Wrap the await in `wait_for`; the existing handler catches `TimeoutError` (it's an `Exception`) and returns `{}`.
- `user_queue_depth` — same shape; `queue_status` ends up `None` and the sidebar hides the banner.
- `user_oldest_pending_row_age_seconds` — already in a nested `try/except`; wrap and let it fall through.

The kill route's `_KILL_CALL_TIMEOUT_SECONDS` stays at 10 s. The poll bound is intentionally looser because poll is read-only and a slow read is less expensive than a slow write.

## Observability (CLAUDE.md rule 14)

New log lines in the poll path (namespace `[route.jobs]` to match existing style):

- `poll_db_call_timeout` — WARNING with `call` (which method timed out), `user_email`, `timeout_s`, `job_id` (for per-job calls like `list_rows`). The single most important signal that Turso is misbehaving.
- The existing `eta_medians_failed` / `queue_status_failed` / `stuck_queued_age_failed` already log on the existing fallback paths — leave them alone, they'll catch the wrapped `TimeoutError` too.

## Settings (CLAUDE.md rule 15)

Add to the runtime settings layer alongside `BULKVID_KILL_CALL_TIMEOUT_SECONDS` (added in d94b01b):

- `BULKVID_POLL_DB_CALL_TIMEOUT_SECONDS` (default 15 s) — soft cap on each libsql roundtrip inside `/jobs/poll`. Admin can raise to 30 s for a known multi-hour Turso slowdown.

Surface in the admin runtime-settings panel under the existing "Resilience" subsection (created by the prior plan), placed next to `kill_call_timeout_seconds`.

## Testing (CLAUDE.md rule 18)

New unit tests in `tests/unit/test_routes_jobs.py`, reusing `_patch_queue_method_to_hang` from the prior plan:

- `test_poll_returns_504_when_list_jobs_times_out` — hang `list_jobs`, expect 504 with diagnostic, no `jobs` payload.
- `test_poll_returns_partial_when_user_queue_depth_times_out` — hang `user_queue_depth`, expect 200 with `jobs` present and `queue_status` null.
- `test_poll_returns_partial_when_eta_medians_times_out` — hang `eta_medians`, expect 200 with `jobs` present and `eta_medians_by_tab == {}`.
- `test_poll_returns_partial_when_list_rows_times_out` — submit a RUNNING job, hang `list_rows`, expect 200 with `jobs` present and `rows_by_job == {}` (job entry omitted).

Apps Script side: no test harness exists in this repo (the existing kill tests only cover the FastAPI route). Manual verification path documented in §Acceptance — clicking Kill on a hung backend now toasts within ≤10 s instead of ≤75 s.

## Security (CLAUDE.md rule 13)

No new attack surface. Both fixes tighten existing endpoints — kill stays the same auth model; the poll route's new timeout cannot be exploited (anonymous callers were already 401'd, no behavior change for them). The new env var is operator-controlled, same trust boundary as the existing kill timeout var.

## Out of scope (V1)

- **Per-call retry inside the poll handler.** A single failed `list_rows` for a single job could be retried once. Skipped because the dropped entry already falls back gracefully (sidebar shows the job card without rows) — adding retry adds latency for marginal UX gain.
- **A "poll degraded" banner in the sidebar.** When `queue_status` comes back null, we could show "queue stats temporarily unavailable" instead of just hiding the banner. Skipped — the existing "Reconnecting…" header in `last-update` already covers the broader case, and a finer-grained banner would clutter the sidebar.
- **Settings UI for `BULKVID_POLL_DB_CALL_TIMEOUT_SECONDS`** in this PR. The env var ships now; the admin-panel surface lands with the next settings refresh (matches how `kill_call_timeout_seconds` was rolled out).

## Alternatives considered (CLAUDE.md rule 4)

1. **Server-side retry for the kill route.** Have the backend retry libsql internally before returning 504. Rejected: the timeout EXISTS because libsql roundtrips can stall arbitrarily; internal retry just hides the symptom and inflates the user-visible wait.
2. **Single 15 s `asyncio.wait_for` around the whole poll handler.** Simpler than five per-call wraps. Rejected because the granularity matters — a single slow `eta_medians` should not block delivery of `jobs` and `rows_by_job`. Per-call wraps preserve the existing graceful-degradation behavior.
3. **Just Gap 1 (Apps Script fast-fail).** Fixes the immediate user complaint but leaves the poll hanging on Turso flaps, which IS the underlying symptom. Both gaps ship together.

## Acceptance

- `pytest tests/unit/test_routes_jobs.py -q` green.
- Manual on HF Spaces after deploy: trigger a poll while a known-bad libsql call is mocked to hang (locally), verify 504 + sidebar reconnect banner. Then in production: when the next "Worker not claiming for Nm" event happens, the kill button surfaces the "Could not kill" toast within ≤10 s (not 75 s) and the sidebar stops sitting on "Loading…" indefinitely.
