# Worker liveness net — DB heartbeat, universal restart, lease sweep, forensics

Date: 2026-08-12
Branch: `worker-universal-liveness-net` (PR 1 of 2)
Status: approved by Yoav (2026-08-12) after a full 5-advisor council pass with
anonymous peer review. PR 2 (the Hrana-over-HTTP transport swap) gets its own
plan when this one is merged.

## The bug, in one sentence

The 2026-08-12 incident wedged with 6 rows frozen in DB `processing` (exactly
`max_concurrent`) plus 109 pending piled up behind them, and every deployed
guard correctly refused to fire because all five gate on `processing == 0` /
`in_flight == 0` / claim FAILURES — a mid-batch freeze is invisible to the
entire zoo, so a human still had to restart the Space by hand.

## Evidence

- HF runtime confirmed prod runs `ad322fd` (both self-heal PRs live) and the
  Space was container-restarted 2026-08-12 08:51 UTC.
- Boot logs: `orphaned_rows_recovered count=6` with `max_concurrent_rows=6`,
  then a healthy drain of ~115 rows in 4 minutes at ~16s/row. The work was
  fine; the process was wedged.
- The exact freeze mechanism is UNCONFIRMED — HF logs only survive since
  container start, and the manual restart destroyed the evidence. Candidate
  mechanisms: a hung event loop (blocks every `asyncio.wait_for` row timer at
  once, which is what "exactly max_concurrent frozen" smells like), a swallowed
  cancellation, or rows stranded in `processing` by the
  `runner_pending_record_giveup` path (recovered only at worker boot today).
- This evidence gap is itself part of the bug: every watchdog kills the process
  holding the answer.

## Why the fix is shaped this way (council outcome, 2026-08-12)

Five advisors + anonymous peer review. Unanimous points:

- A sixth shape-specific watchdog just creates a seventh seam. The lasting
  guard asserts the one invariant "work waiting implies progress" instead of
  enumerating wedge shapes.
- The originally proposed trigger ("zero queue transitions for N minutes") is
  BROKEN and was rejected: six legitimate 25-minute renders produce exactly
  that signature, and the restart-reclaim-restart loop makes duplicate spend
  unbounded. The restart signal must be a worker heartbeat, not queue counts.
- Rows stranded in `processing` deserve a runtime lease sweep, not a restart:
  sweeping them back to `pending` heals the strand with zero cost and re-arms
  the existing idle-only watchdog.
- Forensics must be captured BEFORE any `os._exit`, written somewhere that
  survives the restart (Turso does; HF container logs may not).
- The root cure (transport with real deadlines) is PR 2 — this net stays as
  the backstop for wedge shapes the transport swap cannot reach (hung loop on
  CPU work, unknown future modes).

## Chosen approach

Four pieces, all inside the existing patterns:

### 1. Worker heartbeat written to the DB

- New single-row table `worker_heartbeat` (`id=1`, `epoch` REAL, `pid`,
  `in_flight`). Module-level SSOT functions in `queue.py` (same pattern as
  `count_active_queue`): `write_worker_heartbeat(conn, ...)` and
  `read_worker_heartbeat(conn)`.
- `JobQueue.write_heartbeat(in_flight=...)` async wrapper via `_run_db`.
- `worker.py` starts a background asyncio task: write immediately, then every
  `BULKVID_WORKER_HEARTBEAT_INTERVAL_SECONDS` (default 30). Write failures are
  logged and swallowed — a failed beat IS the signal, never a crash.
- The signal logic: if the event loop hangs, the task stops running; if the
  worker's DB path is wedged, the write fails. Either way `epoch` goes stale.

### 2. Stuck-queue watchdog: heartbeat-stale trigger (the universal net)

- The existing independent daemon thread's fresh-connection probe additionally
  reads the heartbeat epoch.
- New fire condition alongside the existing idle-only one: heartbeat age >=
  `BULKVID_STUCK_QUEUE_WATCHDOG_HEARTBEAT_STALE_SECONDS` (default 240 = 8
  missed beats) AND work is waiting (`pending > 0 OR processing > 0`), on a
  SUCCESSFUL probe. Turso-down still means probe failure -> no restart, so the
  outage discrimination that makes auto-restart safe is preserved.
- Guards: skip condition until process uptime exceeds the stale threshold
  (fresh boot can't false-fire on the previous process's old epoch); a missing
  heartbeat row (first deploy) never fires.
- Age is measured continuously by nature (a landed beat resets it), so no
  streak bookkeeping is needed for this condition.
- Restart cost when it fires mid-batch: up to `max_concurrent` re-run rows —
  identical to the manual restart it replaces, and it only fires when nothing
  has been alive for 4 minutes, i.e. the rows were dead anyway.

### 3. Runtime lease-expiry sweep

- Module-level `sweep_expired_processing_rows(conn, cutoff_iso)` in `queue.py`:
  resets `processing` rows of ACTIVE jobs whose `started_at` is older than the
  cutoff back to `pending` (`started_at = NULL`) — the boot-time
  `recover_orphaned_rows` semantics, run periodically.
- Run from the watchdog thread's fresh connection at most every
  `BULKVID_STUCK_QUEUE_WATCHDOG_SWEEP_INTERVAL_SECONDS` (default 300).
- Cutoff: `BULKVID_STUCK_ROW_SWEEP_AFTER_SECONDS` (default 3600). Deliberately
  a full hour — comfortably above every per-tab row budget (max 1800s) AND
  above plausible admin-raised timeouts, because the settings store may live in
  a separate DB the watchdog cannot read. The sweep is the slow janitor for
  strands; the heartbeat is the fast net for real wedges. If you ever raise a
  per-tab timeout past ~50 min, raise this env too.
- Effect: strands from the `record_result` give-up path no longer disarm the
  idle-only watchdog forever, and no longer wait for a reboot to clear.

### 4. Pre-restart forensics

- New table `wedge_forensics` (ts, process, reason, pending, processing,
  heartbeat_age_s, stacks TEXT), 30-day opportunistic prune on insert (same
  pattern as `kill_audit`).
- New `orchestrator/forensics.py`: format all thread stacks via
  `sys._current_frames()` + thread names (the watchdog lives inside the worker
  process, so this captures the wedged MainThread/event loop — the money
  shot), plus `faulthandler.dump_traceback` to stderr for the live HF log.
- The stuck-queue watchdog dumps stderr + inserts a forensics row (fresh
  connection, time-boxed side thread, ~10s budget, failure never blocks the
  exit) before EVERY `os._exit`. `db_watchdog` gains the stderr dump only (it
  has no DB credentials by design; its stderr survives process-level restarts
  because the container keeps running).
- `/health/deep` gains `worker_heartbeat` (age) and `forensics_recent` (last 3,
  stacks truncated) so the next incident is diagnosable from the browser.

## Alternatives rejected

- **Count-based no-progress trigger** — rejected by unanimous peer review:
  false-fires on legitimate slow batches or is blind for 30+ minutes, and the
  reclaim loop makes duplicate spend unbounded.
- **Dynamic sweep cutoff from the settings store** — the watchdog's jobs-DB
  connection cannot see the settings DB in the two-database Turso deploy; a
  boot-time snapshot goes stale. A generous static default is simpler and
  safe.
- **Retiring the five existing guards now** — deferred until the PR-2
  transport swap has burned in; removal is easy, resurrection mid-incident is
  not.

## Security & safety (rule 13)

- No new external surface, no new secrets, no new user inputs. All new env
  knobs are positive numbers with fallback defaults (never crash on a bad
  value).
- Forensics stacks contain code locations and thread names only — no payloads,
  no tokens, no PII; the table is readable only via the admin-gated
  `/health/deep`.
- Heartbeat carries epoch/pid/in_flight — nothing sensitive.
- Fails safe everywhere: probe failure -> no restart; sweep failure -> logged,
  retried next interval; forensics failure -> logged, exit proceeds; heartbeat
  write failure -> logged, loop continues.
- The restart itself remains `os._exit(1)` + supervisord relaunch — the
  already-accepted convention of all three existing watchdogs.

## Observability (rule 14)

- `worker_heartbeat_write_failed` (WARNING) — beat could not land.
- `heartbeat_stale_observed` (WARNING) — early breadcrumb past 3 intervals.
- `stuck_queue_heartbeat_exit` (ERROR) — the universal-net restart, with age
  and counts.
- `stale_processing_rows_swept` (WARNING) — count + cutoff; the strand-healer
  finally visible in prod.
- `wedge_forensics_written` / `wedge_forensics_failed` (INFO/WARNING).
- `/health/deep`: heartbeat age + recent forensics.

## Settings audit (rule 15)

Env vars, not admin toggles — consistent with every other transport/watchdog
knob:

- `BULKVID_WORKER_HEARTBEAT_INTERVAL_SECONDS` (default 30)
- `BULKVID_STUCK_QUEUE_WATCHDOG_HEARTBEAT_STALE_SECONDS` (default 240)
- `BULKVID_STUCK_QUEUE_WATCHDOG_SWEEP_INTERVAL_SECONDS` (default 300)
- `BULKVID_STUCK_ROW_SWEEP_AFTER_SECONDS` (default 3600)
- Existing `BULKVID_STUCK_QUEUE_WATCHDOG_ENABLED=0` disables the whole thread
  (heartbeat trigger, sweep, and all), unchanged.

Turso free-plan impact: ~86K heartbeat writes/month + one sweep UPDATE per 5
minutes — noise against the 10M monthly write cap.

## Testing (rule 18)

- `test_queue.py`: heartbeat write/read roundtrip and upsert; sweep resets only
  over-cutoff processing rows of active jobs (fresh rows, killed-job rows, and
  pending rows untouched); forensics insert + list + prune.
- `test_stuck_queue_watchdog.py` (patching probe + `_exit_process`, as today):
  stale heartbeat + work waiting fires; stale heartbeat + empty queue does
  not; fresh heartbeat never fires; missing heartbeat row never fires; uptime
  guard suppresses an early fire; probe failure resets everything; existing
  idle-only cases unchanged; forensics attempted before exit.
- `test_worker_wiring.py`: heartbeat task started with the runner.
- `test_routes_health.py`: new fields present and shaped.
- Full `ruff` / `mypy` / `pytest tests/unit` green before PR.

## Deploy (rule 19)

- PR `worker-universal-liveness-net` -> `origin/main`. Prod deploy stays
  Yoav's manual `git push hf main` (GitHub does NOT auto-deploy). Verify with
  the HF API `runtime.sha` after pushing.
- Schema additions are `CREATE TABLE IF NOT EXISTS` — no migration, safe on
  first boot of either process.
- Rollback: revert the PR, or `BULKVID_STUCK_QUEUE_WATCHDOG_ENABLED=0` to
  disarm the net live.

## Documented limitations (v1)

- The rare compound state "stale-read claims + stranded processing rows +
  healthy event loop" self-heals via sweep -> idle-only watchdog, which can
  take up to ~65 minutes with defaults. Accepted: the fast path (heartbeat)
  covers every hung-process shape in ~4 minutes, and PR 2 removes the
  stale-read mechanism entirely.
- A swept row whose original result later lands records the FIRST terminal
  status (usually ROW_TIMEOUT) — same bounded race the boot-time recovery has
  today.
- `db_watchdog` gets stderr forensics only, no DB row (no credentials there by
  design).

## Roadmap after this PR

- PR 2: `db.py` transport swap — stateless Hrana-over-HTTP (`POST
  /v2/pipeline`, httpx, hard connect/read timeouts), behind
  `BULKVID_DB_TRANSPORT`, 48h burn-in with this net armed, then delete the
  libsql client path. Separate plan file.
- After PR 2 burns in: retire the now-redundant shape-specific guards into the
  invariant-based net (fewer restarters, fewer seams).
