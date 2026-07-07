# DB-wedge permanent fix — layered (watchdog-first, transport-timeout root cure)

Date: 2026-07-07
Author: incident triage + 5-advisor council (2026-07-07)
Status: Approved (layered plan + async-hrana Phase 2), pre-implementation
Supersedes the deferred "real cure" in `_plans/2026-07-06-stuck-runs-worker-wedge.md`

## Problem (root cause, verified)

Users must manually restart the HuggingFace Space "way too often". The submit
POST hangs for minutes; a full restart fixes it. Confirmed mechanism:

- Prod uses the **sync** `libsql` client in remote mode
  (`libsql.connect(url, auth_token=...)`, one HTTPS round-trip per statement).
- Verified via Context7 (`/tursodatabase/libsql-python`): remote-mode
  `connect()` exposes **no network/request timeout**, and
  `connection.interrupt()` is **Unimplemented**. A stalled statement (Turso
  flap / half-open TCP) blocks its thread **forever**, uncancellable.
- Every DB call runs as
  `asyncio.wait_for(loop.run_in_executor(dedicated_db_pool, fn), timeout=15s)`
  on a dedicated 16-thread pool in `db.py` (shared by **web and worker**).
  When `wait_for` fires it cancels only the *await*; Python cannot kill the
  thread, so the thread stays blocked on the dead socket. **Each Turso hiccup
  permanently leaks one pool thread.** After ~16 hiccups the pool is fully
  wedged, every DB op queues forever, and only a full container restart
  recovers.
- The web process has no watchdog (a hang is not a crash, so
  `supervisord autorestart` never fires). The shipped worker hard-watchdog is
  opt-in, **off by default, and worker-only** — it cannot save the web
  process, which is the one that hangs the submit.

The 2026-07-06 pass correctly identified the cure ("transport-level deadline /
async libsql client") and **deferred it**, shipping only a bigger pool (raises
the ceiling, still leaks) and an off-by-default worker watchdog. That is why it
still happens.

## Council outcome (5 advisors + anonymous peer review, 2026-07-07)

- **Option A "self-healing DB executor" (swap in a fresh pool+connection on
  leak) is a band-aid — rejected as primary.** Fatal flaw: abandoned pools do
  not free the wedged threads (they park on dead sockets until OS TCP reap,
  15+ min). Under a *sustained* flap it spawns pool after pool → unbounded
  memory → **OOM on the 2-vCPU box → the exact restart it was avoiding**, now
  nondeterministic and mid-request. Also: the pool swap is not atomic
  (split-brain writes in shared code), and a `SELECT 1` health probe can itself
  block forever on the same dead socket (the watchdog sharing the failure mode
  of what it watches).
- **Root cure = a real transport-level timeout**, obtained via the **async
  libsql/hrana client** (has per-request timeouts) — chosen direction — or a
  local timeout-enforcing proxy (rejected: adds a process/failure surface to a
  small box).
- **Embedded replica (First Principles) — unanimous blind spot, rejected.**
  Already tried here; corrupted because web+worker shared one local file path
  (`db.py` history note, 2026-06-04). Only viable with a per-process replica
  path or a single DB-owning process — not worth it.
- **Peer-review catches:** (1) idempotency of in-flight writes is the
  precondition that makes any force-kill/restart safe — mostly already covered
  by idempotency keys + `recover_orphaned_rows`, but must be verified; (2) the
  fix only reproduces under real Turso flaps → needs a fault-injection harness
  to validate; (3) web vs worker are asymmetric (web has no watchdog).

## Goals

- Survive a Turso flap without a manual/HF restart — **both** processes.
- End the "I keep restarting the Space" UX embarrassment.
- Make the next incident diagnosable from logs alone.
- Do no harm: never OOM the box, never split-brain writes, never kill live
  *paid* work on a false positive, never reduce throughput.

## Constraints

- HF Spaces, 2 supervisord processes (web + worker, both `autorestart=true`)
  sharing one remote Turso DB (libsql **remote** mode). Small box (~2 vCPU;
  RAM unconfirmed — open question).
- The sync libsql client gives us no transport timeout and no interrupt.
- A forced restart re-runs in-flight rows (`recover_orphaned_rows` resets
  PROCESSING→PENDING on boot) — bounded duplicate paid-API spend, identical to
  what a *manual* restart already costs today.

## Chosen approach — layered

### Phase 1 — Detect + self-restart (ships first, low risk, ends manual restarts)

Keep the **one** good part of Option A (leaked-call detection) and **drop the
dangerous pool-swap**. Let a dumb `os._exit(1)` + `supervisord autorestart` do
the recovery.

1. **DB pool instrumentation (`db.py`).** Move the per-call deadline into
   `run_db_call` (currently the caller's `wait_for`). Track `in_flight` and
   `leaked` counts: on submit, record start; on internal `wait_for` timeout,
   mark the call **leaked** and attach a `concurrent.futures` done-callback that
   decrements `leaked` if the thread ever finishes. Expose
   `db.db_pool_stats() -> {pool_size, in_flight, leaked}`. No pool swapping —
   detection only.
2. **Shared wedge watchdog (new `orchestrator/db_watchdog.py`).** A plain daemon
   thread (NOT on the DB pool) started at both web and worker boot. Trips —
   loud log then `os._exit(1)` — when `leaked >= pool_size` continuously for
   `T` seconds (default 60s: a fully-wedged pool for a full minute is genuinely
   dead, not merely busy). Reads a counter, so it never blocks on a dead socket
   (fixes the Contrarian's probe critique).
3. **Web-loop liveness heartbeat (belt-and-suspenders).** A background async
   task bumps a monotonic timestamp every ~5s; the watchdog thread also exits if
   the heartbeat goes stale past a threshold (catches a genuinely blocked event
   loop, distinct from a wedged pool).
4. **Enable auto-recovery.** Default the wedge watchdog **ON** (the entire point
   is unattended recovery), env-gated for a kill switch. Keep the existing
   worker claim-failure watchdog as a second, independent signal.
5. **Idempotency safety check.** Confirm (with a test) that a restart re-drives
   in-flight rows without double output — `recover_orphaned_rows` +
   idempotency keys already do this; document and close any gap.

### Phase 2 — Remove the leak at the source (root cure; separate branch, after Phase 1 is live)

- **Feasibility spike: async libsql/hrana client with real per-request
  timeouts** for our remote setup (verify via Context7 + a throwaway probe).
  If it delivers enforceable deadlines, migrate the `db.py` call path behind it
  so a stalled statement raises instead of leaking a thread — the watchdog then
  becomes a rare backstop instead of the primary mechanism.
- If async hrana cannot deliver a real deadline, fall back to the local
  timeout-enforcing proxy (documented, not preferred).

### Explicitly deferred / rejected

- Option A pool-swapping (OOM + split-brain). Rejected.
- Embedded replica (corrupted here). Rejected unless per-process paths.
- Local proxy as *primary* (extra failure surface). Phase-2 fallback only.
- Idempotent KIE-resume (persist task id, resume-poll on reboot) — removes even
  the bounded duplicate-spend of a restart. Deferred follow-up.

## Security / safety (rule 13)

- No new external surface, secrets, or PII/credential logging. New log fields
  are counts. `os._exit` is already used by the existing watchdog; the only new
  risk is re-running in-flight rows after a forced restart — bounded, identical
  to today's manual-restart cost, and strictly better than an indefinite wedge.
  Fails safe: a still-down Turso just restart-loops under supervisord backoff.

## Observability (rule 14)

- `[db pool] stats` line in the heartbeat: `pool_size / in_flight / leaked` +
  `rss_mb`. This is the restart-predictor the current logs lack.
- `[db watchdog] wedge_detected` (loud) before `os._exit`, with the counts that
  tripped it, so a post-mortem points at the exact cause.
- `[db watchdog] heartbeat_stale` when the loop-liveness path trips.

## Settings (rule 15)

New env knobs (operator-tunable, no code change): `BULKVID_DB_WEDGE_WATCHDOG_ENABLED`
(default **on**), `BULKVID_DB_WEDGE_SECONDS` (60), reuse `BULKVID_DB_EXECUTOR_THREADS`.
No user-facing sheet settings — this is infra.

## Testing (rule 18)

- Unit: `db_pool_stats` counts in_flight/leaked correctly across a simulated
  timeout-then-late-finish; watchdog trips past threshold and NOT before;
  heartbeat-stale path; wedge watchdog is a no-op when the pool is healthy.
- Fault-injection harness: a stub `fn` that blocks on an event to simulate a
  wedged libsql call — drive the pool to full leak, assert the watchdog exits
  (patch `os._exit`). This is the only way to validate without a real flap.
- Idempotency: restart mid-batch re-drives PROCESSING rows to one output.
- Full `ruff` / `mypy` / `pytest` green before deploy.

## Deploy (rule 19)

- **Reconcile first:** prod (`hf/main`) is 2 commits ahead of `origin/main`
  (clean fast-forward). PR those into `origin/main` so GitHub = source of truth
  BEFORE branching the fix, else a deploy from local `main` reverts the shipped
  hardening.
- Fix branches off the reconciled `main`; PR → `main`; deploy to `hf` from
  `main` only after CI + review. Never push straight to `main` or `hf/main`.
- Phase 1 and Phase 2 are separate PRs. Watch one full flap cycle after Phase 1
  before starting Phase 2.

## Phase 2 spike findings (2026-07-07)

The gating question — "can we get a real transport deadline?" — turned up a hard
constraint that changes the recommendation:

- The **maintained** `libsql` (libsql-python, what we run) is **sync-only**
  (DB-API 2.0, Rust/PyO3). No async API, no remote timeout, no `interrupt()`.
  Confirmed via Context7.
- The **only** native-async Python client is the separate **`libsql-client`**
  (hrana) — but its last release is **0.3.1, May 2024**, classifiers Python
  **3.7–3.12**. Prod runs **Python 3.14**. Putting the critical DB path on a
  2-year-abandoned dependency on an unsupported Python is a rule-13 red flag.
- Migration *surface* is NOT the blocker: a background-event-loop bridge
  (`run_coroutine_threadsafe(coro, bg_loop).result(timeout)` inside
  `_LibsqlConn`) keeps the sync interface, so the 116 (queue) + 18 (settings)
  call sites stay untouched — only `db.py` changes. The blocker is the *client*.

### Refined options (pick one)

1. **Async `libsql-client` via bg-loop bridge** — genuinely cancellable,
   contained to db.py. CON: abandoned dep on unsupported Python. Risk: HIGH.
2. **Killable subprocess for DB I/O** — SIGKILL a wedged call for real
   cancellation, keeps the maintained sync client. CON: per-call IPC,
   connection re-establish on kill, more moving parts. Risk: MEDIUM.
3. **Local timeout-enforcing proxy** (HTTPS_PROXY → idle-close) — no code
   migration. CON: depends on Rust libsql/reqwest honoring proxy env
   (unverified), new process on the small box. Risk: MEDIUM.
4. **Stop at Phase 1, measure** — the watchdog already turns the wedge from a
   manual restart into an automatic ~70s one, which solves the stated pain. Only
   pursue 1–3 if wedges prove frequent. Risk: NONE (live).

### Reframe (the higher-value follow-up)

Phase 1's only real downside is the **bounded duplicate paid-API spend** when the
auto-restart re-drives in-flight rows. **Idempotent KIE-resume** (persist the KIE
task id, resume-poll on boot) removes that cost entirely — making the watchdog
"free to fire" and Phase 1 genuinely sufficient. This may beat a risky transport
migration on value-per-risk. The council's Expansionist flagged the same.

**Recommendation:** Option 4 now (measure Phase 1 in prod); if wedges are
frequent, do **idempotent resume** before any transport migration; and only if a
transport fix is still needed, prefer Option 2 (killable subprocess, maintained
client) over the abandoned async dep.

## Open questions

- HF box RAM and whether it is OOM-killing (pull the Space metrics at a stall) —
  confirms the OOM critique and sizes the watchdog thresholds.
- How often does the Phase 1 watchdog actually fire in prod? (drives whether any
  of options 1–3 is worth its risk.)
