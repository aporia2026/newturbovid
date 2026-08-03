# Worker stale-read self-heal (the "queued but nothing runs, restart fixes it" wedge)

Date: 2026-08-03
Branch: `worker-stale-read-selfheal`
Status: approved by Yoav (2026-08-03, "start 2"). Council skipped at his request.

## The bug, in one sentence

The worker's long-lived libsql **remote** connection can keep serving a stale
read snapshot — returning `pending=0` for a queue that actually has rows — with
**no error**, so none of the existing self-healers fire and the job sits
`queued` forever until an operator restarts the HF Space by hand.

## Evidence (this is verified, not a theory)

From the 2026-08-02 incident (Omer's screenshot + HF logs):

- Web process (sidebar): "0/6 in flight · **109 queued**" and the "Worker not
  claiming for 1m" banner. That banner only renders when the web connection sees
  `queued > 0 AND in_flight == 0` — a **live** count of pending `row_queue` rows
  joined to an active job ([routes/jobs.py](../src/bulkvid/routes/jobs.py) L1338-L1414).
  So the rows genuinely exist in Turso.
- Worker process (every heartbeat since boot): `pending=0 processing=0
  in_flight=0 idle=True db_wedged=0`. Same remote Turso, same JOIN
  (`_count_active_queue_sync`, [queue.py](../src/bulkvid/orchestrator/queue.py) L1189).
- Both cannot be right. The web view is correct (109 real pending rows, `$0.00`
  spent, nothing ever processed). The worker's connection is reading stale.

## Why nothing auto-recovers today

Three self-healers exist; all three key off an **error** or a **hang**:

1. `JobQueue._run_db` reconnect ([queue.py](../src/bulkvid/orchestrator/queue.py) L1231) — only on an exception.
2. DB-pool wedge watchdog ([db_watchdog.py](../src/bulkvid/orchestrator/db_watchdog.py)) — only when threads hang (`db_wedged` climbs). The logs show `db_wedged=0`.
3. Runner liveness watchdog ([runner.py](../src/bulkvid/orchestrator/runner.py) L662) — only on consecutive claim *failures*. A stale read returns `None` (empty queue) *successfully*, so `_consecutive_claim_failures` stays 0.

A stale read is fast, empty, and throws nothing. It is invisible to every guard.
The `db.py` history note (L501-L507) claims the `isolation_level=None` autocommit
change fixed this class of lag; the incident proves it did not fully. This is a
**distinct failure mode** from the hung-thread wedge that Phase 1 addressed.

## Why the fix must recycle the connection

Verified via Context7 (`/tursodatabase/libsql-python`, 2026-08-03):

- Remote mode exposes **no `sync()`** and **no fresh-read primitive** (`sync()`
  is embedded-replica only; our `_LibsqlConn.sync()` already guards for that).
- `interrupt()` is **unimplemented** (also why a hung call can't be cancelled —
  the Phase 1 finding).

So the only client-exposed way to guarantee the next read sees the latest
committed state is to **close the connection and open a fresh one** — which
`_reconnect_sync` already does. There is no cheaper primitive to reach for.

## Chosen approach (option 2 of 3): stale-read tripwire + self-heal

On the worker's **idle** path, after a bounded stretch of continuous idle,
force a connection recycle and re-count. If the fresh connection now sees
pending rows, we just caught (and healed) a stale read — log it loudly so the
wedge is finally observable. The next claim, on the fresh connection, drains the
backlog.

Mechanism:

1. New public async method `JobQueue.reconnect(*, reason)` — takes `self._lock`
   (so it can never race a concurrent op on the shared connection) and runs
   `_reconnect_sync` on the dedicated DB pool, time-boxed like `_run_db`.
2. `BatchRunner` tracks `_last_stale_read_refresh` (monotonic), reset to "now"
   on any real work and whenever rows are in flight (so the idle clock only runs
   during **sustained true idle**).
3. In the `queued is None` branch, `_maybe_refresh_stale_connection()`:
   - Disabled if `_STALE_READ_REFRESH_SECONDS <= 0`.
   - Skips unless `in_flight_count == 0` (a busy worker is provably reading
     fresh — its `record_result` writes keep the stream live).
   - Skips until `now - _last_stale_read_refresh >= _STALE_READ_REFRESH_SECONDS`.
   - Resets the timer, calls `queue.reconnect()`, then `queue.count_active_queue()`.
   - `pending > 0` after the recycle ⇒ `worker_stale_read_detected` (WARNING).
     Otherwise a routine refresh (DEBUG). Any failure ⇒
     `worker_stale_read_refresh_failed` (WARNING); loop carries on.

Default cadence: **60 s** of idle (env `BULKVID_WORKER_STALE_READ_REFRESH_SECONDS`).
Worst-case wedge duration drops from "until a human notices and restarts"
(minutes to hours) to ~60 s + one poll. The recycle is cheap (one HTTPS
handshake + `CREATE ... IF NOT EXISTS` no-ops) and only happens while idle, so
no in-flight work is ever at risk.

### Alternatives rejected

- **Option 1 (blind periodic reconnect)** — same recycle, but no before/after
  detection. Rejected as the standalone choice because it heals silently; we'd
  never learn the wedge was happening. Option 2 is option 1 plus the tripwire
  log, at the cost of one extra count query per idle minute. Cheap, keep it.
- **Option 3 (async-hrana / transport-deadline rewrite)** — the real root cure,
  also fixes the hung-thread wedge. Weeks of work, high risk. Overkill for this
  specific bug. Stays on the roadmap; Yoav has asked to start it after this ships.

## Architecture / boundaries

- Connection lifecycle stays owned by `JobQueue` (data layer). The runner
  (orchestration) never touches `_conn` or `_reconnect_sync` directly — it goes
  through the new public `reconnect()`, same as every other DB op goes through
  the async wrappers. No boundary crossed.
- SSOT for "recycle the connection" remains `_reconnect_sync`; `reconnect()` is a
  thin lock-guarded async front door, not a second implementation.

## Security & safety

- No new external surface, no new secrets, no new inputs. The env knob is a
  positive float; a bad value falls back to the default (no crash).
- Recycle is gated on `in_flight == 0`, so it can never drop a connection under
  an in-flight paid pipeline. Lock-guarded, so it can never corrupt a concurrent
  claim/record. Fails safe: on reconnect failure the old connection is retained
  (`_reconnect_sync` swaps only after a successful open) and the loop continues.
- No PII or credentials logged (reason strings and counts only).

## Observability (rule 14)

- `worker_stale_read_detected` (WARNING) — the smoking gun: `pending`,
  `processing`, `idle_seconds`, `threshold_s`. This is the line that finally
  proves the wedge in prod and confirms the fix caught it.
- `worker_idle_connection_refreshed` (DEBUG) — routine recycle, nothing stale.
- `worker_stale_read_refresh_failed` (WARNING) — recycle raised; carry on.
- Startup log gains `stale_read_refresh_seconds` so the deploy config is visible.

## Settings audit (rule 15)

Exposed as an **env var** (`BULKVID_WORKER_STALE_READ_REFRESH_SECONDS`), not an
admin-panel toggle — consistent with every other worker/DB plumbing constant
(`_DB_WEDGE_SECONDS`, the watchdog thresholds, the row timeouts' env overrides).
The admin settings store holds operational content (prompts, per-tab row
timeouts), not transport tuning. No user-facing control is warranted.

## Testing (rule 18)

Unit (`tests/unit/`):

- `test_queue.py` (or a new `test_queue_reconnect.py`): `queue.reconnect()`
  swaps the connection — spy `_reconnect_sync`, assert one call, assert the
  handle object identity changed and the queue still works after.
- `test_runner.py`:
  - tripwire fires on sustained idle: old `_last_stale_read_refresh`, fake
    `count_active_queue` returns `(5, 0)` after a spied `reconnect` ⇒ reconnect
    called once + `worker_stale_read_detected` logged with `pending=5`.
  - not-yet-due: recent refresh ⇒ no reconnect.
  - in-flight gate: `in_flight > 0` ⇒ no reconnect.
  - clean refresh: fresh count `(0, 0)` ⇒ reconnect happened, no detect warning.
  - disabled: `_STALE_READ_REFRESH_SECONDS <= 0` ⇒ never reconnects.

Run the full `tests/unit` suite to catch regressions in the runner loop and
queue paths the change touches.

Out of scope for v1 (documented limitation): a stale read that first appears
*mid-batch* (rows in flight the whole time) is not force-refreshed until the
batch drains to idle. The reported incident is fully-idle; option 3 covers the
general case.

## Deploy (rule 19)

- Flow: PR `worker-stale-read-selfheal` → `origin/main` (GitHub, aporia2026).
  Prod deploy is a separate, manual `git push hf main:main` that Yoav triggers —
  **this branch does not touch `main`, `hf`, or production.**
- Rollback: revert the PR; behavior returns to today's (manual restart). The
  env knob can also disable the tripwire live (`=0`) without a code change.
- No schema change, no data migration, no config required for the default to
  take effect.

## Open questions

- Cadence: 60 s default feels right (matches the sidebar's 1-min "not claiming"
  banner). Tunable per-deploy; revisit if prod logs show it's too chatty or too
  slow.
