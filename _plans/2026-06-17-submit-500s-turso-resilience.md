# Kill the "Backend is busy / HTTP 500" submit popup for good — harden the web path against Turso flaps

Date: 2026-06-17
Status: approved (Yoav: stay on Turso Free, fix the code — not a host migration, not a plan upgrade)
Owner: Yoav
Investigation: 2026-06-17 session (code read end-to-end + LLM Council + Turso dashboard)

## Problem (evidence, not guess)

Bulk operators frequently see the Apps Script dialog:

> Backend is busy — the backend is temporarily overloaded and the submit could
> not get through after 6 attempts. Last error: HTTP 500: Internal Server Error.

The Apps Script already retries the submit POST 6× over ~31 s on 5xx/network
errors, with an idempotency key, and *still* surfaces the dialog — so the 500
persists across the whole retry window, not a one-off blip.

### Verified root cause (read from the current code)

1. The stack moved off PythonAnywhere to a HuggingFace Space (Docker +
   supervisord: uvicorn web + worker in one container) backed by **Turso
   (networked libSQL, remote mode)**. Every queue statement is an HTTPS
   round-trip. `_plans/2026-06-04-migrate-to-hf-spaces-turso.md`.
2. `submit_job` → `queue.enqueue` → `_enqueue_sync` makes **N+5 sequential,
   un-retried libsql round-trips** for an N-row batch (the libsql shim unrolls
   `executemany` into N `execute()` calls — commit 5a74f62). There is no real
   transaction (the shim no-ops `BEGIN`/`COMMIT` — commit 928c259).
3. `_enqueue_sync` only catches `sqlite3.OperationalError` → `QueueBusy` → 503.
   But in **Turso remote mode libsql raises its own/grab-bag exceptions**
   (`ValueError: file is not a database`, `wal_insert_begin failed`, timeouts,
   etc. — see commits a2de035, 928c259, ce4cf8d, 62c0950, d94b01b). So that
   `except` **never fires in prod**, and the whole 503 path the
   `2026-06-04-submit-500-defensive-fix` plan built is **dead code on Turso.**
   Any Turso flap during enqueue bubbles **unhandled** → FastAPI default →
   bare `HTTP 500 Internal Server Error`. (The body is exactly Starlette's
   default string, confirming it's our app raising, not HF's router.)
4. `JobQueue` holds **one shared libsql connection, created once in `__init__`
   and never reconnected** (queue.py:232). A mid-statement network failure can
   leave that connection wedged, so every subsequent request 500s until the web
   process restarts — which explains why all 6 client retries over 31 s fail
   identically.
5. There is **no global FastAPI exception handler** (main.py), so any
   unhandled exception anywhere on the web path becomes a bare 500.
6. The **worker** side was already hardened against exactly this across 8+
   commits — hard `asyncio.wait_for` timeout per query + broad `except
   Exception` + a retry/drainer buffer (runner.py, commits 62c0950, d94b01b).
   **The web submit path got none of that.**

### Ruled out (with evidence)

- **Not HF compute / cold start.** Yoav upgraded the HF plan; no change. Turso
  Free already has "no cold starts."
- **Not a Turso plan limit.** Dashboard (2026-06-17, last 30 days): Rows Read
  188.13M / 500M (38%), Rows Written 21.67K / 10M (0.2%), next invoice $0.00.
  Nowhere near any cap. Latency is flat-near-zero except one degradation window
  ~Jun 3–5 (p99 ~480 ms). Upgrading the plan buys monthly quota + backups, not
  fewer transient blips — and every tier still round-trips over the public
  internet. **Plan upgrade rejected: it does not address the failure.**
- **Not "needs more retries."** Already at 6×/31 s. The retries fail because the
  connection is wedged and/or the error is never mapped to a retryable status.

## Goal

Drive the user-visible submit-500 dialog from "frequently" to "effectively
never" for the real-world causes (transient Turso flap, wedged connection,
brief poll-path error), **without** introducing duplicate jobs / duplicate
video generation / duplicate paid-API spend. Honest scope: this makes the
failure invisible and self-healing; it does not claim 100.000% uptime over a
public-internet dependency — a multi-minute total Turso outage would still
delay (not 500) a submit, surfaced as a clear recoverable state.

## Approach

Mirror the worker's proven resilience pattern on the web path, make enqueue
exactly-once by construction, and add a final safety net so nothing is ever a
bare 500.

### Change 1 — DB resilience wrapper in `JobQueue` (the core fix)

`src/bulkvid/orchestrator/queue.py`

- New helper used by the async API methods:
  `async def _run_db(self, fn, *args, op: str, mutating: bool, **kwargs)`:
  - Up to `_DB_MAX_ATTEMPTS` (default 3) tries.
  - Each try: `await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs),
    timeout=_DB_CALL_TIMEOUT_SECONDS)` (default 15 s — loose enough for a slow
    Turso roundtrip, tight enough to beat the Apps Script 30 s UrlFetch cap and
    the per-attempt budget).
  - On `Exception` (incl. `TimeoutError`): log `db_call_retry`, **discard and
    rebuild the connection** via `_reconnect()` (cheap, dumb, safe — we never
    try to "heal" a half-dead socket, we throw it away), backoff with jitter
    (`[0.5, 1.0, 2.0] s`), retry.
  - On exhaustion: raise `QueueUnavailable` (new, subclass of `QueueBusy` so
    existing route `except QueueBusy` blocks keep mapping it to 503).
- `_reconnect()`: close the old `self._conn` (best-effort), call `_db.connect(...)`
  with the stored url/token/path, re-apply `row_factory` + `PRAGMA journal_mode`.
  Guarded by `self._lock` so a concurrent op never sees a half-replaced handle.
- Apply `_run_db` to the **web-facing** methods: `enqueue`, `get_job`,
  `list_jobs`, `list_rows`, `eta_medians`, `user_queue_depth`,
  `user_oldest_pending_row_age_seconds`. (The worker keeps its own existing
  timeout/buffer logic — do not double-wrap its `claim_next_row` /
  `record_result`.)

### Change 2 — Idempotent-by-construction enqueue (correctness; prevents double-billing)

`src/bulkvid/orchestrator/queue.py`

The council's load-bearing catch: making submit retry harder is dangerous
unless a replayed/partial write cannot duplicate rows. Today the only guards
are the `idempotency_keys` lookup (can be missed if the partial write died
before recording it) and the runtime `_active_duplicate_row_nums` query (has
edge holes: rows that already left the active set get reprocessed). Replace
"hope the dedup query catches it" with "duplicates are impossible":

- **Deterministic `job_id` when an idempotency key is present:**
  `job_id = "job-" + sha256(f"{user_email}\n{idempotency_key}").hexdigest()[:16]`.
  A retry computes the **same** `job_id`. (No key → keep today's random id.)
- **Schema:** add `CREATE UNIQUE INDEX IF NOT EXISTS idx_row_queue_job_rownum
  ON row_queue(job_id, row_num);`. Safe to add: job_id is unique per submit and
  row_nums within a job are already distinct, so no existing dupes. Added to
  `_SCHEMA` (runs `IF NOT EXISTS` on every boot — idempotent).
- **Writes use `INSERT OR IGNORE`:** jobs row by PK, row_queue rows by the new
  unique index. A retry of a partially-written batch fills only the missing
  rows; nothing duplicates. The idempotency-key fast-path lookup stays as a
  cheap short-circuit but is no longer the *only* line of defense.

### Change 3 — One batched insert instead of N round-trips

`src/bulkvid/orchestrator/queue.py`

- Replace the unrolled per-row `executemany`/loop with a **single multi-row
  `INSERT OR IGNORE INTO row_queue (...) VALUES (?,?,?,?),(?,?,?,?)...`**
  statement (one `execute()` = one round-trip, one statement so it dodges the
  remote `executemany` no-op bug). Chunk at ~100 rows/statement to stay under
  any statement-size limit. Net: an N-row submit drops from ~N+5 round-trips to
  ~4–6, shrinking the flap surface ~10×. This is an optimization layered on top
  of Change 1/2, not the fix itself.

### Change 4 — Map residual failures to 503 + global exception handler

`src/bulkvid/routes/jobs.py` + `src/bulkvid/main.py`

- Route: `submit_job` already catches `QueueBusy` → 503; `QueueUnavailable`
  inherits from it, so exhausted transient flaps now correctly become 503
  (which the Apps Script retries invisibly). No route signature change.
- `main.py`: add `@app.exception_handler(Exception)` returning **503** with
  `Retry-After: 5` and a fixed JSON body (`{"detail": "backend temporarily
  unavailable, please retry"}`) for any *unmapped* exception — including the
  auth/JWKS verification path, which is the *other* un-hardened network call in
  the submit flow. Re-raise `HTTPException` untouched so 4xx still behave.
  Log `unhandled_exception` with the path + exception type (no PII/secrets).
  This is the final net: even a cause we didn't predict degrades to an
  invisible client retry instead of a popup.

## Alternatives considered and rejected

1. **Upgrade the Turso plan.** Rejected on evidence — dashboard shows 38% reads
   / 0.2% writes / $0 invoice. Tiers change quota + backups, not transient
   reliability. Does not touch the code defect.
2. **Migrate to a Hetzner VPS + local SQLite** (`docker-compose.hetzner.yml`
   already in repo). Genuinely kills the networked-DB failure class, but the
   council was near-unanimous that a single $5 box with no failover, no managed
   backups, and a self-owned 2 a.m. pager is a *worse* availability story than
   managed Turso that recovers itself. Deferred; Yoav declined the ops burden.
3. **Catch specific libsql exception classes.** Rejected: libsql's exception
   surface is undocumented and inconsistent across our own incident history.
   Broad `except Exception` + hard timeout is the only safe catch — and it is
   exactly what the worker already does successfully.
4. **Rely on a libsql transaction / `batch()` for atomicity.** Rejected: the
   shim no-ops `BEGIN/COMMIT`, remote `executemany` no-ops (our own comment),
   and sessions caused stale reads (commit b13c87a). Idempotency-by-construction
   (deterministic ids + unique index + INSERT OR IGNORE) gives exactly-once
   without needing multi-statement atomicity.
5. **Fire-and-forget submit (202 + optimistic "queued" in the sheet, persist
   async).** The strongest *UX* answer (First-Principles + Outsider + Expansionist
   liked it) and the real "instant" experience. Deferred to a v2 — it rests on
   the *same* idempotency work landing first (Change 2), and it adds a
   dead-letter path + sheet reconciliation that is a bigger change than the
   bleeding-stop this plan delivers. Tracked separately.

## Security & safety (Rule 13)

- Deterministic `job_id` is `sha256(user_email + key)` truncated — not
  guessable across users (includes the caller's verified email), and a forged
  key still can't return another user's job because the idempotency lookup +
  job ownership checks remain scoped to `identity.email`.
- The global exception handler returns a **fixed** body — never echoes the
  exception message, SQL, connection string, or token. Internal detail is
  logged server-side only.
- `_reconnect()` reuses the already-validated url/token from `__init__`; no new
  secret handling, nothing logged.
- No new auth surface; idempotency + reconnect run *after* `get_identity`.
- The new unique index cannot leak data; it only constrains writes.

## Observability (Rule 14)

New server logs (namespace `[bulkvid queue]` / `[bulkvid boot]`):
- `db_call_retry` — `op`, `attempt`, `of`, `reason` (exception type), `wait_s`.
- `db_reconnect` — `op`, `reason`. A nonzero rate is the real Turso flap rate
  we were previously eating as 500s.
- `queue_unavailable_503` — `op`, `attempts`. Fires only when all retries +
  reconnects still failed (genuine outage window).
- `unhandled_exception` — `path`, `exc_type`. Should trend to ~0; any entry is
  a cause we didn't anticipate.

## Testing (Rule 18)

`tests/unit/test_queue_resilience.py` (new):
- `test_enqueue_retries_then_succeeds` — fn raises a generic Exception once,
  then succeeds; assert one reconnect, one job, correct row_count.
- `test_enqueue_exhausts_raises_queue_unavailable` — fn always raises; assert
  `QueueUnavailable` (and that it is a `QueueBusy` subclass).
- `test_enqueue_timeout_reconnects` — fn sleeps past the timeout; assert
  `TimeoutError` is caught, reconnect happens, then 503 path.
- `test_enqueue_idempotent_replay_no_dupes` — same key twice → identical
  `job_id`, exactly one set of `row_queue` rows (unique index holds).
- `test_enqueue_partial_then_retry_fills_missing` — simulate first attempt
  inserting a subset, retry with same key completes the set, no duplicates.

`tests/unit/test_routes_jobs.py` (extend):
- `test_submit_queue_unavailable_503` — queue raises `QueueUnavailable` →
  503 + `Retry-After`.
- `test_unhandled_exception_returns_503_not_500` — force a non-HTTPException in
  a route → global handler returns 503, fixed body, no leak.

Run the **full** suite green before declaring done. Manual smoke:
- Local (sqlite mode): submit a 3-row image_vo job; submit again immediately
  (same selection) → one job, three rows, no dupes.
- Monkeypatch the queue conn to raise once on enqueue → submit still succeeds
  (server self-healed), no dialog.

## Rollout

1. Land Changes 1–4 + tests. Backward compatible (old Apps Script without a key
   still works; the unique index + INSERT OR IGNORE are no-ops for it).
2. Push to GitHub; HF Space rebuilds.
3. Watch logs 24–48 h: `db_reconnect` / `queue_unavailable_503` show the true
   flap rate; `unhandled_exception` should be ~0; the user-facing dialog should
   go quiet.

## Open questions

- Poll-path reads (`list_jobs` / `list_rows` inside `poll_jobs`) aren't wrapped
  in the route's degrade-gracefully try/except. Change 1 wraps them at the queue
  layer, and Change 4 nets them at the app layer — confirm that's sufficient or
  add a route-level guard.
- v2 fire-and-forget/instant submit tracked separately (see Alternative 5).
