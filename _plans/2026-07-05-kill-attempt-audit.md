# Durable kill-attempt audit

Date: 2026-07-05
Owner: Yoav
Status: **Approved — in progress**

## Context (the incident this fixes the forensics for)

2026-07-05 morning: Evgeny submitted a 96-row `simple_motion` job (04:09 UTC),
tried to cancel it repeatedly, and reported "the kill option didn't work no
matter what I tried — it generated 100 videos for nothing" ($25.28 spent).

Post-incident investigation established:

- Not a single kill transition hit the DB that morning (job never moved to
  `killed`, zero rows aborted), while kills from the SAME sheet + script
  worked five days earlier (three `killed` jobs on 2026-06-30).
- The backend and Turso were healthy the whole run (the worker claimed and
  recorded all 96 rows continuously).
- The HF Spaces container restarted at 05:19:56 UTC, and both the per-job log
  file (`/tmp`) and the runtime stdout logs are ephemeral — **every trace of
  the failed kill attempts was gone** by the time anyone could look.

Root cause of the kill failure itself: still unknown, BECAUSE there was no
durable evidence. That's the gap this plan closes: a failed kill on a
running paid job must never again leave zero forensics.

(Separately, the deployed Apps Script was missing the fast-fail kill fix from
`_plans/2026-06-14-fast-fail-kill-and-poll-timeout.md` §A — committed to git
but never `clasp push`ed. Pushed on 2026-07-05. Not part of this plan.)

## Goal

Every kill *attempt* that reaches the backend — success or failure — is
recorded durably in Turso, with its outcome, so the next "kill didn't work"
report can be answered from data instead of guesswork:

- Rows present with failure outcomes → the request arrived and failed
  server-side (timeout / busy / auth), and we know which and when.
- No rows at all → the request never left the client; investigate Apps
  Script / sidebar, not the backend.

## Constraints

- Audit writes must be **best-effort and non-blocking**: a slow or dead DB
  must never delay or fail the kill itself beyond a small bounded cost.
- No new infrastructure, no new paid services. Same FastAPI + Turso.
- Backwards compatible: existing kill semantics, status codes, and response
  shapes unchanged.

## Design

### D.1 Schema — new `kill_audit` table (in `_SCHEMA`, idempotent)

```sql
CREATE TABLE IF NOT EXISTS kill_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,       -- ISO-8601 UTC, time the attempt arrived
    endpoint    TEXT NOT NULL,       -- kill_job | kill_all_jobs | admin_kill_job
    job_id      TEXT,                -- NULL for kill-all
    user_email  TEXT NOT NULL,       -- authenticated caller (admin: "admin:<basic-user>")
    outcome     TEXT NOT NULL,       -- see D.2
    detail      TEXT                 -- rows_aborted on success, error text on failure
);
CREATE INDEX IF NOT EXISTS idx_kill_audit_ts ON kill_audit(ts);
```

### D.2 Outcome vocabulary

| outcome | meaning |
|---|---|
| `received` | attempt logged, final outcome never recorded (finalize write lost — itself evidence of a DB flap mid-kill) |
| `killed` | job(s) transitioned to killed; `detail` carries jobs/rows counts |
| `no_active_job` | kill landed but nothing was queued/running (e.g. clicked after the job finished) |
| `timeout` | the kill DB call exceeded `_KILL_CALL_TIMEOUT_SECONDS` → 504 |
| `queue_busy` | `QueueBusy`/`QueueUnavailable` → 503 |
| `forbidden` | ownership check failed → 403 |
| `not_found` | unknown job_id → 404 |
| `error` | any other unexpected failure |

### D.3 Queue methods (`queue.py`)

Sync helpers next to the existing kill helpers, async wrappers next to
`kill_job` / `kill_all_jobs`, all routed through `_run_db`:

- `record_kill_attempt(*, endpoint, job_id, user_email) -> int` — INSERT with
  `outcome='received'`, returns the audit row id. Opportunistically prunes
  rows older than `KILL_AUDIT_TTL_SECONDS` (90 days) after the insert,
  mirroring the idempotency-prune pattern.
- `finalize_kill_attempt(audit_id, *, outcome, detail=None)` — UPDATE.
- `list_kill_attempts(limit=100) -> list[KillAttempt]` — newest first, for
  the admin page. `KillAttempt` is a small frozen dataclass beside `Job`.

### D.4 Route wiring (`routes/jobs.py`, `routes/admin.py`)

Two tiny route-layer helpers own the "best-effort" contract:

- `_kill_audit_start(queue, ...) -> int | None` — `asyncio.wait_for` bound by
  `_KILL_AUDIT_DB_TIMEOUT_SECONDS` (default 5 s, env-overridable). On ANY
  failure: WARNING log, return `None`, kill proceeds unaudited.
- `_kill_audit_finish(queue, audit_id, ...)` — no-op when `audit_id is None`;
  same bound + swallow.

`kill_job` / `kill_all_jobs` / admin `kill_job` record at entry (after auth)
and finalize on every exit path (success, TimeoutError, QueueBusy,
HTTPException 403/404). The 401 layer is intentionally NOT audited — it fires
in the auth dependency before the handler, and an unauthenticated attacker
should not be able to write rows to our DB. Trade-off documented below.

Latency cost when Turso is healthy: two extra roundtrips per kill (~100-400
ms). When Turso is flapping: at most +10 s on top of the kill's own 10 s
budget — still inside the Apps Script 30 s UrlFetch cap.

### D.5 Admin surface

New page `GET /admin/kills` — table of the last 100 attempts (time, endpoint,
job link, user, outcome badge, detail). Nav link "Kill audit" in `_base.html`.
Read-only; same HTTP Basic gate as the rest of the panel.

## Security (rule 13)

- No new attack surface: recording happens only after Bearer auth (jobs
  routes) or HTTP Basic (admin route); anonymous callers still 401 before any
  write. The admin page is read-only behind the existing Basic gate.
- `detail` stores error strings truncated to 300 chars — no tokens, no
  payloads, no PII beyond the already-stored user email.
- Not auditing 401s means a credential-stuffing attacker leaves no rows in
  this table — deliberate (they'd be writing to our DB); uvicorn access logs
  still show them while the container lives.

## Observability (rule 14)

New `[queue]` / `[route.jobs]` log lines:

- `kill_audit_recorded` (DEBUG) — `audit_id`, `endpoint`, `job_id`, `user_email`
- `kill_audit_write_failed` (WARNING) — the audit itself failed; `endpoint`,
  `stage` (record|finalize), `error`. The kill continues.
- `kill_audit_pruned` (DEBUG) — `removed` count.

The audit table IS the durable observability; these lines only cover the
audit's own failure modes.

## Settings (rule 15)

- `BULKVID_KILL_AUDIT_DB_TIMEOUT_SECONDS` (default 5.0) — env var beside the
  existing kill/poll timeouts. Not surfaced in the admin runtime-settings
  panel for now: it's a resilience tuning knob with a sane default, same
  rollout shape `kill_call_timeout_seconds` had (env first, panel later).
- Retention (90 days) intentionally hardcoded: kill attempts are tiny rows in
  the tens-per-month range; a knob would be clutter. Revisit only if volume
  surprises us.

## Testing (rule 18)

`tests/unit/test_queue.py`:
- record → finalize → list roundtrip (fields, ordering, newest-first).
- prune removes rows older than TTL, keeps fresh ones.

`tests/unit/test_routes_jobs.py`:
- kill success → one audit row, `outcome=killed`, detail carries rows count.
- kill of already-finished job → `outcome=no_active_job`.
- kill timeout (hang `kill_job`) → 504 AND audit row `outcome=timeout`.
- kill 403 (non-owner) → audit row `outcome=forbidden`.
- kill-all → audit row `endpoint=kill_all_jobs`, no job_id.
- **audit failure never breaks the kill**: patch `record_kill_attempt` to
  raise → kill still succeeds (bug-shape regression guard).

`tests/unit/test_routes_admin.py`:
- `/admin/kills` renders attempts; admin kill records `endpoint=admin_kill_job`.

Full `pytest` suite green before done.

## Deploy (rule 19)

Backend-only change — ships as a PR into `main`; merging triggers nothing by
itself (HF Space deploys when pushed to the `hf` remote). No Apps Script
change. Schema migration is a pure additive `CREATE TABLE IF NOT EXISTS` at
connection open — zero risk to existing data, rollback = revert the commit
(the orphaned table is inert). No env changes required (the new timeout var
has a default).

## Alternatives considered (rule 4)

1. **Persist all route logs to Turso.** Answers more questions but turns
   every request into extra DB writes and re-invents a log store on the hot
   path. Rejected: kills are the only action where lost evidence costs real
   money (the $25 incident); scope to them.
2. **Client-side audit (Apps Script writes attempts to a hidden sheet).**
   Would capture attempts that never reach the backend — the one blind spot
   left. Rejected for V1: sheet writes from the sidebar add quota load and a
   second audit source of truth; Evgeny's answer to "did you see an error
   alert?" covers the client side for this incident. Revisit if a client-side
   failure is confirmed.
3. **Audit middleware for all kill-path status codes incl. 401.** Lets
   unauthenticated traffic write DB rows. Rejected — see Security.

## Out of scope

- Surfacing audit rows in the operator sidebar (admin-only surface for now).
- Auditing job submissions (idempotency table already provides that trail).
- Alerting on failed kills (worth a follow-up once we see real data).
