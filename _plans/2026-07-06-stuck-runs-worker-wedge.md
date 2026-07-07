# Stuck runs — worker wedge hardening

Date: 2026-07-06
Author: incident triage (users reporting runs stuck 30+ min, "nothing happens")
Status: proposed → council → implement

## Problem

Users report runs stuck for 30+ minutes with no progress. The container log for
the reported window (`07:10:56` boot → `10:12` first job) shows a **healthy**
worker: three hours idle on an empty queue, then a normal pickup of a 14-row
`simple_motion` job. It is truncated right as processing starts, so it does not
itself contain a stuck run. But it does contain two hard signals:

- A full-container restart at `07:10:56` (supervisord pid 1, both `web` and
  `worker` respawned) — something restarted the whole container.
- `SQLITE_NOMEM` (Turso server-side out-of-memory) on the settings DB at
  `09:07`, self-healed via reconnect — proof Turso flaps in this deploy.

## What the code rules out

Every row is hard-capped by `asyncio.wait_for(processor, timeout=<tab budget>)`
in the runner (`simple_motion` = 1200 s / 20 min). Article fetch, KIE poll,
`download_image`, Rendi and ZapCap are each independently time-boxed. So **no
single row can hang past 20 minutes.** "30+ minutes, nothing happens" is
therefore a *system-level* wedge (or a UI-read failure), not one slow row.

## Root-cause hypotheses (ranked)

1. **Concurrency too high for the box.** Prod runs `max_concurrent=20`
   (`worker_start max_concurrent_rows=20`); the config default is 10, itself
   "tuned for a CPU-quota-aware small box." 20 amplifies memory pressure (each
   `simple_motion` row buffers full MP4/image bytes in RAM; 20 in parallel can
   OOM a small HF box → the `07:10` restart) and thread-pool contention.
2. **Thread-pool exhaustion under a Turso flap.** All 41 `asyncio.to_thread`
   calls share the **default** executor = `min(32, cpu+4)` ≈ 6 threads on a
   2-vCPU Space. When Turso flaps, DB calls wedge inside libsql (uncancellable);
   6 wedged threads exhaust the pool and every later DB op queues forever. The
   existing watchdog is gated off whenever rows are in flight or results are
   buffered (`runner.py::_maybe_watchdog_exit`) — i.e. exactly during an active
   batch — so it cannot self-heal mid-batch. Code already documents this risk
   (`runner.py` Prong 2 comment).
3. **KIE throughput starvation.** `kie_pool_init key_count=1`. One key + 20
   concurrent rows × up to 2 image + 2 video KIE calls → 429 storms → 60 s
   cooldowns → rows crawl; some hit the 20-min ceiling. Feels stuck; is "slow +
   timeouts". Operational, not a code bug.
4. **UI-read stall.** Turso flap makes `GET /jobs/{id}` return 503; the sidebar
   freezes on last-known state while the worker may be fine. Some "stuck"
   reports are presentation-layer, not processing.

Common thread across 1–3: **`max_concurrent=20` is too aggressive for this box**
and amplifies every failure mode.

## Goals

- Make the worker survive a Turso flap without a manual/HF restart.
- Make the *next* incident diagnosable from logs alone (today `idle=True
  in_flight=0` looks identical whether the queue is empty or the worker is
  silently failing to claim pending rows).
- Reduce the pressure that triggers the wedge in the first place.
- Do no harm: never kill in-flight *paid* work on a false positive, never
  reduce throughput.

## Constraints

- HuggingFace Spaces, two supervisord processes (web + worker) sharing one
  remote Turso DB (libsql **remote** mode — strongly consistent, no replica).
- Small box (assume 2 vCPU until confirmed). Memory is the scarce resource.
- Duplicate paid-API spend is the cost of any restart that re-runs in-flight
  rows (`recover_orphaned_rows` resets PROCESSING→PENDING on boot).

## Fixes

### Code (this repo)

1. **Larger `to_thread` pool at startup (worker + web).** Install a
   `ThreadPoolExecutor` as the loop default executor, size via
   `BULKVID_WORKER_THREAD_POOL_SIZE` (default 32). Decouples DB round-trips from
   the CPU-derived default; directly defuses hypothesis 2. Also makes fix 3
   (watchdog) safe: with ample threads, a *continuous* claim failure means Turso
   is genuinely unreachable, not merely thread-starved.

2. **Pending-depth in `runner_heartbeat`.** Add a cheap `pending`/`processing`
   count (new lean `JobQueue.count_active_queue()`) to the heartbeat line. Pure
   observability, zero behavior change. Distinguishes "empty queue" from "rows
   stranded in PENDING".

3. **Hard-wedge watchdog (careful — council this).** Add a *second*, higher
   threshold `BULKVID_WORKER_WATCHDOG_HARD_MAX_CONSECUTIVE_CLAIM_FAILURES`
   (default 20 ≈ 10+ min of continuous claim failure) that force-exits
   **unconditionally** (ignores the in-flight / buffered gates). Rationale: if
   claims fail continuously for 10+ min, the worker is genuinely wedged; the
   in-flight rows cannot be progressing either (same dead resources), so
   restarting is strictly better than staying stuck forever. The existing
   conservative watchdog (threshold 6, gated on nothing-to-lose) stays as the
   fast path. Counter resets on *any* claim success (incl. empty-queue `None`),
   so an intermittently-reachable Turso never trips it. Cost: bounded duplicate
   spend on the handful of in-flight rows re-run after reboot.

4. **KIE starvation observability (safe).** Emit a throttled
   `kie_pool_all_keys_cooling` warning when `KiePool.acquire` has to sleep
   because every key is in cooldown. Makes throughput starvation visible. **No**
   concurrency semaphore — one that held through the 5-min poll would reduce
   throughput; the pool already rotates keys + cools on 429.

5. **Move CTA Pillow render off the event loop.** `render_cartoon_cta_overlay_bytes`
   is called synchronously on the loop in 4 processors (cartoon, yt_cartoon,
   avatar, simple_motion); under high row concurrency these serialize and block
   *all* rows' async I/O. Wrap each in `asyncio.to_thread` (safe now that fix 1
   gives us headroom). Pure function (bytes→bytes), low risk.

### Operational (HF Space env — cannot be done from the repo; hand to operator)

- **Lower `BULKVID_MAX_CONCURRENT_ROWS` from 20 → 6–8.** Single highest-leverage
  lever; cuts memory, thread, and KIE pressure at once.
- **Add KIE keys** (raises `key_count` so `create_task` actually rotates).
- **Check the HF tier's memory/restart history**; if it OOMs under normal
  batches, price a larger tier before upgrading (recurring monthly cost — flag,
  don't upgrade blind).

## Alternatives rejected

- *KIE concurrency semaphore around submit+poll* — holds a slot through the
  ~5-min poll wait, so it throttles throughput without relieving the real
  (rate-limit) bottleneck. Rejected in favor of more keys + observability.
- *Watchdog that fires on any claim failure while in-flight* — false-positives
  on a merely-busy (thread-starved) worker would kill live paid work → duplicate
  spend. Rejected; the hard threshold + fix 1 close that gap.
- *Lower the code default for `MAX_CONCURRENT_ROWS`* — the default is already 10;
  the problem is the env override to 20. Fixed operationally, not in code.

## Security / safety (rule 13)

- No new external surface, secrets, or logging of PII/credentials. New log
  fields are counts and key *suffixes* (already logged).
- The hard watchdog uses `os._exit` (already used by the existing watchdog);
  the only new risk is re-running in-flight rows after a forced restart, which
  is bounded and strictly preferable to an indefinite wedge. It fails *safe*
  (a still-down Turso just restart-loops under supervisord backoff).
- Thread-pool size is bounded and env-capped; it cannot grow unbounded.

## QA

- `ruff`, `mypy`, `pytest` green.
- Unit tests: heartbeat includes pending count; hard watchdog fires past the
  hard threshold regardless of in-flight and NOT before it; conservative
  watchdog unchanged; `count_active_queue` returns correct pending/processing.
- Verify the thread-pool executor is actually installed as the loop default at
  worker startup.

## Council outcome (5-advisor review, 2026-07-06)

The council reframed the plan and it was revised accordingly:

- **Fix 1 became a dedicated bounded DB executor**, not a bump of the shared
  default pool. Bumping the shared pool only "raises the ceiling" and a Turso
  flap could still starve rendering; isolating DB I/O contains the blast radius
  and makes claim-failure a clean wedge signal. (The OOM objection to more
  threads was checked and refuted: concurrent MP4 buffering is capped by the
  row semaphore, not the thread count.)
- **The watchdog is a band-aid; the real cure is an enforceable DB deadline**
  (transport-level timeout / async libsql so `wait_for` genuinely cancels and
  threads stop leaking). Shipped the watchdog **opt-in / OFF by default** with a
  hard deploy-order rule: never enable before Fix 1 is live.
- **Idempotent KIE-resume is the meta-fix**: persist the KIE task id and
  resume-poll on recovery so a restart-driven re-run costs $0 — which makes the
  watchdog free to fire. Deferred (bigger change).
- **Measure first**: RSS was added to the heartbeat so the OOM-vs-thread-wedge
  question can be answered from logs.

## Shipped this pass (code)

Fix 1 (dedicated DB executor: `db.run_db_call` + `get_db_executor`, routed from
`queue._run_db` and the three settings-store DB calls), Fix 2 (heartbeat
`pending`/`processing` depth via `queue.count_active_queue` + `rss_mb`), Fix 3
(opt-in hard watchdog, `BULKVID_WORKER_HARD_WATCHDOG_ENABLED` default off), Fix 4
(`kie_pool_all_keys_cooling` throttled warning), Fix 5 (CTA render → `to_thread`
in all four processors). New env knobs: `BULKVID_DB_EXECUTOR_THREADS` (16),
`BULKVID_WORKER_HARD_WATCHDOG_ENABLED` (off),
`BULKVID_WORKER_HARD_WATCHDOG_MAX_CONSECUTIVE_CLAIM_FAILURES` (20).

## Deferred (recommended follow-up, needs go-ahead)

- **Idempotent KIE-resume** (persist task id, resume-poll on reboot) — removes
  the watchdog's duplicate-spend cost. The prerequisite for turning the hard
  watchdog ON safely.
- **Transport-level / async DB deadline** so a wedged libsql call is truly
  abandonable and threads never leak — the root cure that would make the
  watchdog essentially never fire. Needs a libsql capability check (Context7 /
  docs) before committing.

## Operator actions (cannot be done from the repo)

1. **Lower `BULKVID_MAX_CONCURRENT_ROWS` 20 → 6-8** (env). Highest-leverage,
   zero-code, deploy first, alone. Restart the Space on the new value.
2. **Add KIE keys** so `create_task` actually rotates under load.
3. **Pull the HF Space memory/restart graph at a stall** to confirm OOM vs
   thread-wedge. Do NOT enable the hard watchdog until Fix 1 is live AND
   idempotent-resume ships.

## Open questions

- Actual box size (vCPU/RAM) and whether HF is OOM-killing — confirms fix 1/ops
  sizing. Needs the operator to check the Space metrics.
- For a specific stuck job: are its rows `pending` (never claimed) or
  `processing` (claimed, hung)? Splits hypothesis 1/2 from 3 cleanly.
