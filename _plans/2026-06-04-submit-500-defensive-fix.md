# Stop the visible "HTTP 500" toasts on submit: idempotency + wider retry window

Date: 2026-06-04
Status: approved (Yoav picked "Defensive fix today, decide on host later")
Owner: Yoav
Investigation transcript: 2026-06-04 morning session

## Problem (evidence, not guess)

Bulk users see frequent `Submit failed: HTTP 500: Internal Server Error` toasts in
the sheet sidebar after clicking "Generate selected rows" or "Generate all
unprocessed". The Apps Script already retries 3 times with short backoff (≈1.8 s
total); the user still sees the error.

### What the prod logs prove (PA, 2026-06-04 06:47–07:06)

- `error.log`: zero Python tracebacks. The only entries are successful JWK cert
  fetches. Nothing in the app code is raising.
- `server.log`: exactly **one** `job_submit` line in the window — the cartoon
  submit at `06:47:38`, which **succeeded**. After that, only `verify_ok` from
  steady sidebar polling. The user's later failed submit attempts produced
  **no log entry at all** — not even a `verify_ok` — meaning they never reached
  the FastAPI app.
- The Apps Script error text is the bare string `Internal Server Error`, not
  the JSON `{"detail": ...}` shape FastAPI returns. That's PA's WSGI frontend
  message, not ours.

### Verified root cause

This is the **same class of failure** the previous plan
`_plans/2026-06-04-fix-sidebar-500s.md` diagnosed — PA's uWSGI frontend returns
500 when it cannot dispatch the request to a free worker in time. That plan
cut polling traffic by ~5×, which fixed the high-frequency 500s on poll, but
**the dispatch flakiness itself is still there** and continues to hit lower-
frequency endpoints like `POST /jobs` whenever:

1. All 5 PA workers happen to be busy (lazy-initing, recycling, or handling
   concurrent polls from multiple users).
2. The Apps Script's 1.8-second retry budget runs out before any worker frees
   up.

The previous plan considered moving off PA and rejected it for lack of
evidence. We now have that evidence: same failure mode, different endpoint.
Yoav has chosen to defer the migration but wants the visible toasts gone now.

### What this plan is NOT

Not the cure. PA's flaky dispatch will still exist after this lands. The cure
is moving off PA (Oracle Cloud Always Free Tier or PA Developer paid plan,
tracked separately). This plan stops the user-visible bleeding so we can
decide on the host without time pressure.

## Goal

Drive the visible 500-toast rate on submit from "again and again" to "almost
never" by:

1. Making `POST /jobs` **safe to retry** (so a retry that PA's frontend
   dropped doesn't create a duplicate job).
2. **Widening the Apps Script retry window** for submit from ≈1.8 s to ≈31 s,
   covering the normal range of PA worker recovery time (cold-start + retry
   bounce).
3. Mapping server-side SQLite `OperationalError` (lock contention) to
   `HTTP 503 + Retry-After`, so the client retries those instead of bubbling.
4. Better user-facing error when all retries do eventually fail — clear
   "PythonAnywhere is temporarily overloaded, click to retry" copy with a real
   retry button, not the cryptic "HTTP 500".

## Approach

### Change 1 — Idempotency key in submit payload

**Client (`apps_script/Code.gs`)**

- Generate a random ID per submit click:
  `'sub-' + Date.now() + '-' + Utilities.getUuid().slice(0, 8)`.
- Stash it in `DocumentProperties` under `LAST_SUBMIT_KEY` **before** firing
  the POST (so a mid-call Apps Script crash leaves the key recoverable).
- Include it in the payload: `payload.idempotency_key = key`.
- On success, clear the property.

**Server (`src/bulkvid/routes/jobs.py` + new table in `queue.py`)**

- New table `idempotency_keys (key TEXT PRIMARY KEY, user_email TEXT NOT NULL,
  job_id TEXT NOT NULL, created_at TEXT NOT NULL)`.
- New `JobQueue.lookup_idempotency_key(user_email, key) -> str | None` — returns
  the prior `job_id` for the same user if seen.
- `submit_job` flow:
  1. If `payload.idempotency_key` present, look it up scoped to
     `identity.email`. If found, return the **same** `SubmitJobOut` shape with
     `status="queued"` (we don't try to refetch the actual job status — the
     client will see the live status via the next poll). Log `idempotency_hit`.
  2. Otherwise, enqueue as today, then record the key.
- Both writes (idempotency row + jobs/row_queue) happen inside the same
  `_tx()` so a crash mid-write cannot leave the key recorded without the job.
- Key format validation: must match `^[A-Za-z0-9_-]{1,64}$`. Reject anything
  else with 400 (prevents storage-abuse via huge keys).
- TTL: a `prune_old_idempotency_keys` helper deletes rows older than 24 h,
  called opportunistically on enqueue (cheap; one `DELETE WHERE created_at <
  ?` per enqueue, scoped by the timestamp column).
- Backward compatible: if `idempotency_key` is missing, behaviour is exactly
  as before (a `null` field on the Pydantic model).

### Change 2 — Wider, smarter Apps Script retry for POST `/jobs`

**Client (`apps_script/Code.gs`)**

- Generalize `_fetchJson(path, options, retryOpts)` to accept a per-call retry
  policy. Default stays at 3 attempts × `[0.6 s, 1.2 s]` (today's behaviour
  for polls/log fetches — unchanged).
- For the submit POST only, pass `{ maxAttempts: 6, backoffMs: [1000, 2000,
  4000, 8000, 16000] }`. Worst case ≈ 31 s, enough for a PA worker to recycle
  and the dispatcher to find a free worker.
- Add 503 to the retry set (currently 5xx is retried; 503 already qualifies,
  this just makes it explicit).
- On final failure after all attempts, show a user-friendly dialog:
  "PythonAnywhere is temporarily overloaded. Click Retry to try again, or
  Cancel to do it later." with two buttons. Retry re-sends the **same**
  idempotency key — so any submit that *did* succeed on the backend simply
  returns the existing job (no duplicate). Cancel leaves the key in
  `DocumentProperties` so the next click can resume.

### Change 3 — Server: map `OperationalError` → `503 Retry-After`

**Server (`src/bulkvid/routes/jobs.py` + `src/bulkvid/orchestrator/queue.py`)**

- New exception `JobQueue.QueueBusy` (or a top-level `class QueueBusy
  (RuntimeError)`).
- Wrap the `_enqueue_sync` body in `try / except sqlite3.OperationalError as
  e: raise QueueBusy(str(e)) from e`. Same for `_kill_job_sync`,
  `_kill_all_sync` — anywhere we hold a write lock.
- Route handler in `submit_job` (and `kill_job`, `kill_all_jobs`) catches
  `QueueBusy` and raises `HTTPException(503, "queue temporarily busy",
  headers={"Retry-After": "5"})`. Log `queue_busy_503`.
- This is defense-in-depth — the previous plan ruled out SQLite contention as
  the *cause* of today's 500s, but mapping the exception to 503 anyway means
  if it ever does happen, the client retries gracefully.

### Change 4 — Lightweight pre-warm before submit (optional, behind a flag)

**Client (`apps_script/Code.gs`)**

- Before the POST, fire a `GET /health` with a short 5 s timeout and
  `muteHttpExceptions: true`. This nudges PA to wake up a worker if one is
  cold. We do NOT block submit on the response — submit always fires.
- Skip the pre-warm if we did one within the last 60 s (stored in
  `DocumentProperties`).
- Behind a small `PREWARM_ENABLED` constant in `Code.gs` (default `true`),
  flippable without redeploy if it ever proves counterproductive.

## Alternatives considered and rejected

1. **Just bump the existing retry count from 3 to 6** with no idempotency
   key. Rejected: without an idempotency key, a retry of a POST that *did*
   succeed on the server (but whose response PA's frontend dropped) creates a
   duplicate job. Today we dodge this by the dedup-by-row-num check in
   `_enqueue_sync`, but that produces a second "job-…" with 0 rows that
   confuses the sidebar. An idempotency key is the right primitive.
2. **Server-side replay-detection via `(sheet_id, worksheet, row_nums,
   created_at_within_5s)`**. Rejected: brittle (relies on row-num overlap),
   user-confusing (replay across sessions could be a real new request), and
   already mostly subsumed by the existing dedup. Idempotency key is cleaner.
3. **Switch the submit POST to a fire-and-forget pattern** (POST returns
   202 + job_id immediately, work continues in background). Rejected: today's
   submit is already fast (the slow part is the worker draining the queue,
   which is already async). The 500s are dispatch-layer, not work-layer.
4. **Move off PA now.** Tracked, but explicitly deferred by Yoav. Plan is
   here as a band-aid until the migration ships.
5. **Apps Script `try { ... } catch` swallows the toast and silently retries
   forever.** Rejected: violates rule 10 (lazy user must know what's
   happening). The retry policy MUST be bounded and end in a clear dialog.

## Security & safety (Rule 13)

- **Idempotency key is per-user**: lookup is scoped to `identity.email`, so
  user A's key cannot return user B's job. Enforced at the SQL level by
  including `user_email` in the lookup `WHERE`.
- **Key format validation**: regex `^[A-Za-z0-9_-]{1,64}$` rejects oversized
  or non-ASCII payloads. Returns 400 with no key echoed back (no reflected
  XSS surface, though this is a JSON endpoint anyway).
- **TTL of 24 h** on the idempotency table — bounds replay-attack window if
  a key ever leaks. Even within the window, attacker would have to also pass
  identity verification as the same user, since lookup is scoped.
- **No new auth surface** — idempotency check runs *after* the existing
  `get_identity` dependency, not before.
- **No PII or token added to logs** — the key is opaque and short.
- **503 mapping does not leak SQLite internals** — the response body is a
  fixed string `"queue temporarily busy"`; the original
  `OperationalError` message is logged server-side only.
- **`muteHttpExceptions: true` on pre-warm** means a pre-warm failure cannot
  leak request internals to the Apps Script execution log, which is shared
  with non-admin users via View > Executions.

## Observability (Rule 14)

Namespace `[bulkvid route.jobs]` unless noted.

Server-side new logs:
- `idempotency_hit` with `user_email`, `key`, `prior_job_id` — proves how
  often PA's frontend is dropping responses after the worker has already
  processed. We expect a non-zero count after deploy; that's the bug being
  papered over.
- `idempotency_recorded` with `user_email`, `key`, `job_id`, `row_count`.
- `idempotency_key_rejected` with `user_email` and `reason` (`malformed`,
  `too_long`) — defense-in-depth visibility on abuse attempts.
- `queue_busy_503` with `endpoint`, `original_error` — fires whenever
  `OperationalError` actually trips. We expect zero today; if non-zero we
  learn something.

Client-side observability is limited (Apps Script's `console.log` goes to the
Apps Script execution log, not the user). The user-visible signal is the
retry dialog itself.

Apps Script-side new logs (visible in View → Executions):
- `[bulkvid submit] start key=<key> rows=<n>`
- `[bulkvid submit] attempt <i> ok` / `attempt <i> http500 retry-in <ms>`
- `[bulkvid submit] final-fail after <i> attempts` (when the dialog appears)
- `[bulkvid prewarm] hit / skip` for the optional pre-warm.

## Settings audit (Rule 15)

No user-visible settings. Internal constants only:
- `SUBMIT_MAX_ATTEMPTS = 6` in `Code.gs`.
- `SUBMIT_BACKOFF_MS = [1000, 2000, 4000, 8000, 16000]` in `Code.gs`.
- `PREWARM_ENABLED = true` in `Code.gs`.
- `PREWARM_COOLDOWN_MS = 60000` in `Code.gs`.
- `IDEMPOTENCY_TTL_SECONDS = 86400` in `queue.py`.

Surfacing these as admin settings would be premature — users don't care, and
they'd never tune them correctly. Flagged here per the rule. If we ever see
evidence that backoff timing needs per-tenant tuning, revisit.

## Testing (Rule 18)

New `tests/unit/test_routes_jobs.py` cases (uses the existing FastAPI
TestClient fixture):

- `test_submit_idempotency_replay_returns_same_job` — POST same key twice,
  asserts identical `job_id`, asserts only one `jobs` row exists in the DB.
- `test_submit_idempotency_scoped_per_user` — user B replays user A's key →
  treated as a brand-new submit, gets a different `job_id`.
- `test_submit_idempotency_malformed_key_400` — bad characters, oversized key.
- `test_submit_without_key_backward_compatible` — payload without the field
  still works (no change in behaviour).
- `test_submit_503_on_sqlite_operational_error` — monkeypatch the queue to
  raise `sqlite3.OperationalError`; assert 503 + `Retry-After: 5`.
- `test_kill_503_on_sqlite_operational_error` — same for the kill endpoint.

New `tests/unit/test_queue_idempotency.py`:
- `test_idempotency_key_prune_ttl` — insert rows with old `created_at`,
  assert prune removes them.
- `test_idempotency_lookup_returns_none_for_unknown` — non-existent key
  returns `None`, not an exception.

Run the full suite (current count: 395 + new) before declaring done. The
existing 395 must stay green.

Manual smoke checklist:
- Run web app + worker locally; submit a 3-row image_vo job; confirm sidebar
  shows it queued.
- Open Apps Script editor; trigger submit twice in rapid succession
  (re-select rows, click Generate, click Generate again) — only one job
  should appear in the sidebar.
- In a separate shell, hold a `BEGIN IMMEDIATE` lock on `jobs.db`; submit;
  assert the sidebar shows the polite "PythonAnywhere temporarily overloaded"
  dialog after ≈31 s, with a working Retry button.

## Rollout

1. Server changes first (idempotency table, route logic, 503 mapping, tests).
   Backward compatible — old Apps Script without the key still works.
2. Apps Script changes second (key generation + property persistence +
   widened retry + dialog + pre-warm).
3. Push to GitHub.
4. PA pull + reload web app. Always-on task does NOT need restart (no worker
   changes).
5. Re-deploy Apps Script bound to the sheet.
6. Watch `server.log` for 24 h:
   - `idempotency_hit` rate tells us how often PA's dispatch is actually
     dropping responses after success (the "hidden" 500s the user *was* seeing
     without realizing they were near-misses).
   - `queue_busy_503` rate tells us whether SQLite contention is real.
   - `job_submit` (existing) rate vs. `idempotency_hit` rate: if `hit` is
     significant, the cure (Oracle Cloud / paid PA) becomes urgent.

## Open questions

- None blocking. Migration to Oracle Cloud / paid PA is tracked separately.
