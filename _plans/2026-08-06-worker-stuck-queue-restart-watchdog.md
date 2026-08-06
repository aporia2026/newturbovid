# Stuck-queue restart watchdog (automate the manual "restart the Space")

Date: 2026-08-06
Branch: `worker-selfheal-restart`
Worktree: `C:/Projects/turbovid-new-worker-selfheal-restart`
Status: approved by Yoav (2026-08-06) via the two design questions — threshold ~4 min,
idle-only scope. Council skipped (mirrors an already-accepted in-repo pattern:
`os._exit` + supervisord, used by both existing watchdogs).

## The bug, in one sentence

Users still hit the "queued but nothing runs" wedge (163 rows queued, 0 in flight,
"Worker not claiming for 7m") that the 2026-08-03 stale-read tripwire was supposed
to kill — because the tripwire *detects* the wedge but has **no escalation** when
its own cure (an in-process connection recycle) fails to defeat it.

## Why the four existing self-healers all miss this

| Guard | Fires on | Why it misses |
|---|---|---|
| `_run_db` reconnect | an exception | a stale read throws nothing |
| DB-pool wedge watchdog | a hung thread (`db_wedged` climbs) | stale read is fast, `db_wedged=0` |
| liveness watchdog | consecutive claim *failures* | a stale read "succeeds" with `None` |
| stale-read tripwire (PR #27) | 60s idle → recycle + re-count | recycles, logs `worker_stale_read_detected`, then **trusts the next claim drains it**. If the fresh in-process `libsql.connect()` is *also* stale (Turso routing the new remote socket to a lagging replica, or the wedge recurs immediately), the worker loops forever: reconnect → see 163 → log → idle → claim `None` → repeat. "7m" = ~7 detect cycles, no restart. |

Every guard keys off a specific **cause** on the worker's own connection. None keys
off the ground-truth **outcome** from an independent vantage point. A full HF/process
restart fixes it because a brand-new process gets a genuinely fresh libsql client;
an in-process reconnect evidently does not always achieve the same freshness.

## Chosen approach: independent daemon-thread stuck-queue watchdog

A sibling of `db_watchdog.py`. A plain daemon thread — deliberately **not** on the
event loop and **not** on the shared DB pool/connection — that:

1. Every `_CHECK_INTERVAL_SECONDS` (default 30s) opens its **own fresh short-lived
   connection** and counts the active queue: `(pending, processing)`.
2. Fires `os._exit(1)` — supervisord `autorestart=true` relaunches a clean worker in
   ~3s (web process stays up) — only when it can **independently prove**, via a
   *successful* fresh read, that `pending > 0 AND processing == 0` has held
   continuously for `_STUCK_SECONDS` (default **240s / 4 min**).

Why this succeeds where the other four fail:

- **Independent connection** → immune to the main connection's stale snapshot; it
  sees what the web app sees (the plan's evidence: web saw 109, worker saw 0 on the
  same Turso).
- **Off the event loop** → survives even a fully-hung loop (a case the in-loop
  tripwire cannot cover at all).
- **Keys off the outcome** ("real work waiting, nothing draining"), not any single
  cause → covers stale-read-that-reconnect-can't-cure, dead loop, and unknown
  future modes with one mechanism.
- **Applies the proven remedy** (a fresh process) → literally automates the manual
  restart Yoav keeps doing.

### The safety property that makes auto-restart safe

The watchdog fires **only on a successful fresh read** showing `pending>0 &&
processing==0`. This cleanly discriminates the two cases:

- **Stale-worker-connection wedge** (Turso reachable, worker's view wrong): the
  watchdog's fresh read *succeeds* and shows the real backlog → restart, and it
  works.
- **Turso genuinely down** (nobody can read): the watchdog's fresh read *fails* →
  it can't confirm a backlog and does **not** restart (a restart wouldn't help a
  down DB anyway). This is what prevents restart-hammering during an outage.

### Gates (all must hold to fire)

- `processing == 0` — idle-only. Nothing in flight ⇒ **zero paid work lost** on
  restart. Matches the exact screenshot (0/6 in flight). A worker at its
  concurrency cap has `processing == max_concurrent > 0`, so saturation never
  trips it. Long-running rows (a 20-min cartoon) have `processing > 0`, so they
  are never killed.
- `pending > 0` on a **successful** fresh read — real, active-job rows are waiting.
- Sustained ≥ `_STUCK_SECONDS` (monotonic first-seen timestamp; reset the instant
  a poll is not-stuck or the read fails) — well past any real Turso flap, which the
  reconnect heals in seconds.

## Architecture / boundaries (rule 20)

- **SSOT for the queue-depth query**: extract `queue.count_active_queue(conn)` as a
  module-level pure function; `JobQueue._count_active_queue_sync` becomes a thin
  caller (`return count_active_queue(self._conn)`), and the watchdog calls the same
  function on its own fresh connection. The wedge query lives in exactly one place.
- **Connection construction** stays owned by `db.connect` (data layer). The watchdog
  uses `db.connect(...)` + `count_active_queue(...)`; it does not hand-write SQL or
  reach into `JobQueue` internals.
- The watchdog does not import the runner or the event loop — it is a pure,
  self-contained restart backstop.

## Security & safety (rule 13)

- No new external surface, no new secrets, no new inputs. Env knobs are positive
  numbers; a bad value falls back to the default (no crash).
- Reads only counts (no PII, no payloads, no credentials logged — only the redacted
  host via existing `db.connect` logging).
- Fails safe: a failed/timed-out probe never restarts; it resets the stuck timer.
- `os._exit(1)` (not `sys.exit`) bypasses interpreter cleanup so a wedged process
  can't hang on the way out — same convention as the two existing watchdogs.

## Observability (rule 14)

- `stuck_queue_watchdog_start` (INFO) — thread started, with thresholds.
- `stuck_queue_watchdog_disabled` (INFO) — kill switch env off.
- `stuck_queue_observed` (WARNING) — a stuck poll: `pending`, `processing`,
  `stuck_seconds`, `threshold_s`. The breadcrumb trail toward a restart.
- `stuck_queue_probe_error` (WARNING) — fresh read failed/timed out; no action.
- `stuck_queue_hard_exit` (ERROR) — the restart, with the counts and duration that
  justified it. This is the line that finally names the auto-restart in prod.

## Settings audit (rule 15)

Env vars, not admin-panel toggles — consistent with every other worker/DB plumbing
constant (`_DB_WEDGE_SECONDS`, the existing watchdog thresholds, the stale-read
cadence). No user-facing control warranted.

- `BULKVID_STUCK_QUEUE_WATCHDOG_ENABLED` (default on; `0` disables)
- `BULKVID_STUCK_QUEUE_WATCHDOG_THRESHOLD_SECONDS` (default 240)
- `BULKVID_STUCK_QUEUE_WATCHDOG_CHECK_INTERVAL_SECONDS` (default 30)
- `BULKVID_STUCK_QUEUE_WATCHDOG_PROBE_TIMEOUT_SECONDS` (default 15)

Distinct names from the web banner's `BULKVID_STUCK_QUEUED_THRESHOLD_SECONDS`
(no collision).

## Testing (rule 18)

New `tests/unit/test_stuck_queue_watchdog.py`, patching the probe + `os._exit`
(exactly like `test_db_watchdog.py`):

- fires after threshold when probe reports `(pending>0, processing=0)` sustained.
- does NOT fire before threshold.
- does NOT fire when `processing > 0` (in flight), even with `pending > 0`.
- does NOT fire when `pending == 0`.
- does NOT fire when the probe raises/times out (Turso-down case); stuck timer
  resets.
- a transient stuck poll followed by a healthy poll resets the timer (no fire).
- `enabled=0` ⇒ thread never starts / never fires.

Plus a `test_queue.py` assertion that `count_active_queue(conn)` returns the same
`(pending, processing)` as `JobQueue.count_active_queue()` (SSOT refactor is
behavior-preserving). Run the full `tests/unit` suite.

## Deploy (rule 19)

- Flow: PR `worker-selfheal-restart` → `origin/main` (GitHub, aporia2026). Prod
  deploy is a separate, manual `git push hf main:main` that Yoav triggers — **this
  branch does not touch `main`, `hf`, or production.**
- Rollback: revert the PR (behavior returns to today's manual restart), or disable
  live with `BULKVID_STUCK_QUEUE_WATCHDOG_ENABLED=0` — no code change.
- No schema change, no data migration.

## Documented limitations (v1)

- **Mid-batch wedge** (rows frozen in `processing` the whole time) is intentionally
  out of scope — restarting there re-runs paid rows. The existing opt-in hard
  watchdog covers that deliberately-riskier case.
- If a probe *hangs* (Turso unresponsive on the fresh connect), that cycle is a
  no-op (the timed-out probe = the don't-restart case). A stale read is fast and
  never hangs, so the target wedge is always probe-visible.
- If a future change makes the kill switch (or any admin pause) actually gate
  claiming, this watchdog must be taught to skip while paused — today the kill
  switch does not gate claiming, so no such gate exists to honor.

## Verification still owed (rule 1)

Structural gap confirmed from code; the *specific* 2026-08-06 incident mechanism
should be confirmed against HF worker logs (grep `runner_heartbeat`,
`worker_stale_read_detected`, `db_watchdog`). The fix is safe and robust regardless,
but the logs turn "should catch it" into "verified it would have."
