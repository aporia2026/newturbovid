# Worker self-heal from a wedged Turso connection (reconnect + watchdog)

Date: 2026-06-22
Status: approved, implementing
Related: `_plans/2026-06-17-submit-500s-turso-resilience.md` (web-path hardening this mirrors),
`_plans/2026-06-04-migrate-to-hf-spaces-turso.md` (remote-mode rationale),
`_plans/2026-06-14-stuck-processing-rows.md` (record_result retry buffer)

## Problem

Operators repeatedly see the sidebar banner "Worker not claiming for 1m" with rows
queued and `0 / N in flight`, and the only known fix is to manually restart the
Hugging Face Space. It is NOT a Hugging Face capacity problem and NOT a Turso quota
problem; upgrading either plan does not change it.

### Verified root cause

- The web app and the worker run in ONE HF Spaces container under supervisord, both
  talking to Turso in libsql REMOTE mode (every statement is one HTTPS round-trip,
  Hrana protocol). Hrana streams expire after ~10s of inactivity and drop on any
  network blip, and the Python libsql client does NOT transparently reconnect.
  Confirmed against the libsql issue tracker ("The stream has expired due to
  inactivity", STREAM_EXPIRED; documented workarounds are reconnect-on-failure or a
  keepalive ping).
- The WEB path is already hardened: `JobQueue._run_db` time-boxes each call and on ANY
  failure discards the connection, opens a fresh one, backs off, and retries
  (`queue.py`).
- The WORKER hot loop is NOT hardened. `claim_next_row`, `record_result`, and
  `recover_orphaned_rows` do `async with self._lock: await asyncio.to_thread(sync_fn)`
  on the single shared connection with no reconnect. When the stream dies, every claim
  hangs, the runner's `asyncio.wait_for` fires, it backs off 2s, and retries on the
  SAME dead connection forever. It never reconnects, so it never recovers.
- Compounding: `asyncio.wait_for` cancels the coroutine but cannot kill the underlying
  thread. Orphaned `to_thread` threads stay blocked inside libsql and accumulate. The
  default `ThreadPoolExecutor` on a 2-vCPU box is only ~6 threads, so a handful of
  stuck threads can starve all `to_thread` work, after which even a reconnect attempt
  cannot get a thread.
- supervisord `autorestart=true` only restarts a process that EXITS. A hung-but-alive
  worker never exits, so supervisord never restarts it. Only a human clicking HF
  "Restart" opens a fresh process (fresh connection + fresh thread pool).

## Goals

1. The worker recovers from an expired/dropped Turso stream on its own, with no human
   restart, the same way the web path already does.
2. As a last-resort backstop, a wedged worker that cannot recover in place gets a
   clean process restart automatically (via supervisord), WITHOUT losing money or
   producing duplicate videos.

## Constraints / requirements

- Reuse the proven web-path machinery (`_run_db`), do not invent a second divergent
  reconnect path. (The existence of two divergent paths is the underlying bug.)
- The runner loop must never crash; it is the last line of defense.
- Each in-flight row can represent real paid-API spend (kie.ai, Gemini TTS, Rendi). A
  restart must never discard in-flight work or completed-but-unrecorded results,
  because `recover_orphaned_rows` would reset those rows to PENDING on the next boot
  and reprocess them (duplicate videos, duplicate spend).
- Minimal, surgical change. Keep the existing record_result retry buffer + sheet
  write-back behavior intact.

## Chosen approach (two prongs, defense in depth)

### Prong 1 — worker reconnect (the real fix)

Route the worker's three hot-path queue calls through the existing `_run_db`
discard-and-reconnect+retry wrapper:

- `claim_next_row`  -> `_run_db(self._claim_next_row_sync, op="claim_next_row")`
- `record_result`   -> `_run_db(self._record_result_sync, queue_id, result, op="record_result")`
- `recover_orphaned_rows` -> `_run_db(self._recover_orphaned_rows_sync, op="recover_orphaned_rows")`

CRITICAL: `_run_db` already acquires `self._lock`. The current methods ALSO acquire
`self._lock`. `asyncio.Lock` is NOT reentrant, so the call-site `async with self._lock`
MUST be removed when delegating to `_run_db`, otherwise the worker deadlocks on its
first claim. (Caught by the council peer review, verified in `queue.py`.)

Because `_run_db` now owns time-boxing + reconnect + retry for these calls, remove the
runner's now-redundant outer `asyncio.wait_for(..., _WORKER_QUERY_TIMEOUT_SECONDS)`
wrappers around claim, record_result (in `_handle_row` and in the drainer), and
get_job (get_job already uses `_run_db`). Keep every broad `except`, the backoff, and
the `_pending_records` buffering. This makes "one place owns DB time-boxing and
recovery" true for web AND worker. Remove the now-dead
`_WORKER_QUERY_TIMEOUT_SECONDS` constant; keep the inter-claim backoff constant.

On sustained unreachability `_run_db` raises `QueueUnavailable`; the runner's existing
broad `except` catches it, counts it (see prong 2), backs off, and continues. On a
transient flap `_run_db` reconnects and the same call returns successfully, so the
runner never even sees an error.

### Prong 2 — gated liveness watchdog (last-resort backstop)

Add a consecutive-failure counter on the runner:

- Increment on a claim that fails (any exception out of `claim_next_row`).
- Reset to 0 on ANY successful claim, INCLUDING one that returns `None` (an empty-queue
  result still proves the connection is alive).
- When `consecutive_failures >= threshold` AND `in_flight_count == 0` AND
  `pending_records_count == 0`: log a structured `runner_watchdog_exit` and call
  `os._exit(1)` so supervisord relaunches a clean process.

The two gates are the safety guarantee:
- `in_flight_count == 0` -> no row is mid-pipeline, so no paid work is killed.
- `pending_records_count == 0` -> no completed-but-unrecorded result is in the
  in-memory buffer, so the restart cannot cause `recover_orphaned_rows` to reprocess a
  finished row (no duplicate videos / spend).

Threshold is env-tunable (`BULKVID_WORKER_WATCHDOG_MAX_CONSECUTIVE_CLAIM_FAILURES`,
default 6). At roughly 30-47s per failed `_run_db` cycle plus backoff, 6 failures is
~5 minutes of continuous unreachability with nothing in flight — comfortably past any
transient flap (which prong 1 heals in one cycle, resetting the counter).

`os._exit` (not `sys.exit`) is deliberate: it bypasses interpreter cleanup so a wedged
process cannot hang during shutdown.

## Alternatives considered and rejected (for now)

1. Connection-per-operation (open + close a fresh libsql connection per DB call).
   This is the FUNDAMENTAL fix the council's First-Principles and Contrarian advisors
   argued for: a stream that is never reused long enough cannot expire, and the shared
   lock + orphaned-thread accumulation both disappear. Rejected for this change because
   it rewrites the DB layer for web AND worker, adds a connect handshake (extra remote
   round-trip) per op, and is materially riskier. Recommended as the next iteration
   after we measure the per-op round-trip cost. Reconnect-on-failure gets us the
   reliability now at a fraction of the risk.
2. Embedded-replica mode. Already tried and abandoned (two processes sharing one
   container's local replica file corrupted each other; see the migrate plan). Not
   revisiting.
3. Keepalive ping. The worker already polls `claim_next_row` every 1s while idle, which
   keeps the stream warm during idle. It does NOT cover hard drops or the busy phase
   where the claim loop parks on the semaphore. Reconnect-on-failure covers all of
   those, so a separate keepalive adds little.
4. Split the worker into its own container/Space. Would make crash-restart trivial, but
   HF Spaces is one container per Space; splitting is a deployment + cost change out of
   scope here. Noted as a future option.
5. Routing `kill_job` / `kill_all` through `_run_db` too. Reasonable consistency win
   (kill is the user's escape hatch), but it is a web-path call on a different process
   and the route already 504s a hung kill with a clear message. Left out to keep this
   change focused on the worker wedge; noted as a follow-up.

## Known residual risks (documented, not fixed here)

- Half-dead stream that returns success with stale/empty reads (claims return `None`
  forever though rows exist, no error fires). Prong 1 sees no error to trigger
  reconnect, and the watchdog counter keeps resetting on the `None` "successes", so
  neither catches it. The autocommit design (`isolation_level=None`, every statement
  its own transaction) already makes stale reads far less likely than the old
  session-holding mode. A true fix is a heartbeat row write-and-read-back as the
  liveness signal; deferred to avoid adding write load every cycle.
- During a genuine multi-minute Turso OUTAGE the watchdog will restart the worker
  every ~5 min. With both gates satisfied this is harmless churn (nothing in flight,
  nothing buffered), and supervisord treats each as a normal RUNNING->EXITED restart
  (the worker runs well past `startsecs=3` before wedging), so it does not trip
  `startretries` into FATAL.

## Security / safety

- No new external surface, no new secrets, no new logging of credentials or PII. The
  watchdog log line carries only counters (consecutive failures, in_flight,
  pending_records) — no payloads.
- Fail-safe: the watchdog's gates mean the failure mode of a bad threshold is "worker
  does not restart" (status quo, manual restart still works), never "worker destroys
  in-flight paid work". Prong 1 fails safe too: on exhaustion it raises a handled
  `QueueUnavailable`, the loop survives.
- `os._exit` only ever runs behind the two gates and the threshold; it cannot fire
  during normal operation.

## Test plan

- claim self-heals: first `_claim_next_row_sync` raises, reconnect fires, retry returns
  the row (mirror `test_enqueue_retries_then_succeeds`).
- claim exhausts: persistent failure raises `QueueUnavailable`; the runner loop counts
  it and does not crash.
- record_result routes through `_run_db` and reconnects (no deadlock — proves the lock
  was removed correctly).
- watchdog fires only when `consecutive >= threshold` AND `in_flight == 0` AND
  `pending_records == 0` (monkeypatch `os._exit`).
- watchdog does NOT fire when `in_flight > 0` or `pending_records > 0`, even past the
  threshold.
- counter resets on a successful claim that returns `None`.
- full `pytest` suite green (especially `test_queue_resilience.py`,
  `test_runner.py`).
```
