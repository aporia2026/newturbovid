# Stuck "running" jobs: derived state, one-shot kill, self-repair, restart button

Date: 2026-08-17
Status: approved (chat 2026-08-17) — implementing
Branch: `worktree-selfheal-stuck-jobs`

## Goal

Three things the operator asked for, in priority order:

1. **Fix the bug.** Jobs whose every row is finished keep showing `running` in
   the sidebar and resist the Kill button.
2. **A restart button in the Sheet** that restarts the HuggingFace Space,
   because the operator is away for several days.
3. **As much unattended self-fixing as possible**, plus one-click repair
   buttons, with the full repair log visible in the sidebar.

## Root cause of bug 1 (confirmed by reading the code, not inferred)

There are **no transactions in production**. `db.py` translates
`BEGIN`/`COMMIT`/`ROLLBACK` into no-ops for both remote transports
(`_LibsqlConn.execute`, `_HranaConn.execute`) — autocommit was forced on
2026-06-04 to kill stale reads — and the Hrana transport makes every statement
an independent HTTP request with a 10 s read deadline. So `queue.py`'s
`with self._tx():` blocks are **sequences of independently-failing statements**,
not transactions.

`_record_result_sync` runs four of them:

1. `SELECT job_id, status FROM row_queue WHERE id = ?`
2. `UPDATE row_queue SET status = done|failed, finished_at, result WHERE id = ?`
3. `UPDATE jobs SET completed_rows = completed_rows + 1, cost_usd = cost_usd + ?`
4. `UPDATE jobs SET status = completed WHERE completed_rows + failed_rows >= row_count AND status = running`

and it returns early when statement 1 reports the row is already terminal — a
guard added by `_plans/2026-06-14-stuck-processing-rows.md` so a late success
can't overwrite a kill.

Those two facts combine into a permanent wedge:

* statement 2 lands, statement 3 raises (transport deadline, 5xx, flap) →
  `_run_db` reconnects and re-runs the whole function → the guard sees `done`
  and **returns before statements 3 and 4 ever run**. The counter is
  permanently one short, so the finalize condition can never be met again.
* statement 3 lands, statement 4 raises **on the last row of a job** → no
  further `record_result` will ever fire for that job, so nothing re-attempts
  the finalize.

Either way: every row `done`, job pinned at `running` forever. The sidebar
renders active cards from `status IN (queued, running)`, so the card never
leaves, and its progress reads `4 / 5` while the sheet already holds every
video. This became much more reachable after the 2026-08-12 Hrana transport
landed, because a stalled statement now *raises on a deadline* (by design)
where the old client blocked — every raise is a retry, and every retry of this
function after a partial write is a permanent counter loss.

The same non-atomicity means `_claim_next_row_sync` can mark a row
`processing` and then fail to promote its parent `queued → running`, which
strands the job as `queued` while its rows are in flight — the poll only
fetches per-row detail for `running` jobs, so that card reads "waiting in
queue" forever.

## Root cause of bug 2 ("cannot be stopped or killed")

`_abort_rows_for_kill_sync` issues **one UPDATE per aborted row** (the result
JSON embeds each row's `row_num`) plus one per parent job. Under the HTTP
transport each of those is a separate round-trip. A 100-row kill is 100+
sequential round-trips; at a realistic 100 ms each that is 10 s+, and the kill
route is bounded at `_KILL_CALL_TIMEOUT_SECONDS = 10.0`. The route raises 504,
`Code.gs` deliberately does not retry kills, and the operator sees
"Could not kill". Worse: statement 1 (`jobs → killed`) already landed, so a
retry finds nothing active, reports `killed: false`, and leaves the rows
stranded `processing` under a killed parent — invisible to
`sweep_expired_processing_rows`, which only touches active jobs.

## Approach

### A. Derived state, not accumulated state (fixes bug 1)

`completed_rows` / `failed_rows` stop being counters that get incremented and
become values **recomputed from `row_queue`** in one idempotent statement:

```sql
UPDATE jobs SET
  completed_rows = (SELECT COUNT(*) FROM row_queue rq
                    WHERE rq.job_id = jobs.job_id AND rq.status = 'done'),
  failed_rows    = (SELECT COUNT(*) FROM row_queue rq
                    WHERE rq.job_id = jobs.job_id AND rq.status = 'failed')
WHERE job_id = ?
```

Running it twice is the same as running it once, so a retry after a partial
write converges instead of drifting. The finalize then stops doing counter
arithmetic and asks the only question that actually matters:

```sql
UPDATE jobs SET status = 'completed', finished_at = ?
WHERE job_id = ? AND status IN ('queued','running')
  AND EXISTS     (SELECT 1 FROM row_queue WHERE job_id = ?)
  AND NOT EXISTS (SELECT 1 FROM row_queue WHERE job_id = ?
                  AND status IN ('pending','processing'))
```

The `EXISTS` clause is load-bearing: without it a job created milliseconds ago,
whose `row_queue` rows have not been inserted yet, has "no non-terminal rows"
and would be marked complete with zero output. Both clauses ship in the shared
helper so every caller (inline finalize, repair sweep) gets the guard.

`cost_usd` keeps accumulating behind the existing terminal-row guard. It is
money-cosmetic, and under-reporting on a lost statement is the safe direction;
deriving it would need `json_extract` over `row_queue.result`, which is an
unverified dependency on Turso's JSON1 build and not worth the risk in the same
change as the fix.

`row_count` is **never rewritten.** It is the only number in the row that
cannot be reconstructed (rows the client never managed to insert are gone), so
a mismatch is reported in the repair log and left alone. A finished job
honestly reading `3 / 5` is information; a silently rewritten denominator is a
lie.

Because an inflated `row_count` must not block the heal, finalize also accepts
a job whose present rows are all terminal once it is past a grace age
(`BULKVID_REPAIR_MIN_JOB_AGE_SECONDS`, default 120 s) even when
`COUNT(rows) < row_count`.

### B. One-shot kill (fixes bug 2)

`_abort_rows_for_kill_sync` collapses to a single set-based UPDATE that builds
the per-row result JSON in SQL:

```sql
UPDATE row_queue SET status='failed', finished_at=?,
  result = '{"row_num":' || row_num || ',"status":"KILLED_BY_USER",...}'
WHERE status IN ('pending','processing') AND job_id ...
```

Only `row_num` — an INTEGER column — is interpolated, so there is no quoting or
escaping surface and no injection vector; every other byte is a literal. (This
is why `||` is used rather than `json_object()`, which would add an unverified
dependency on the JSON1 extension being present in Turso's build.) Counters are
then refreshed by the derived-state statement from §A.

Kill cost becomes **4 round-trips regardless of batch size**, well inside the
10 s route budget, for both `/jobs/{id}/kill` and `/jobs/kill-all`.

### C. Repair module (`orchestrator/repair.py`)

Six discrete, individually idempotent actions, each a plain function taking a
connection so they can run on the watchdog's own fresh connection *and* behind
the endpoint with no duplicated logic:

| action | what it fixes |
| --- | --- |
| `finalize_settled_jobs` | the bug 1 wedge: active job, no non-terminal rows |
| `promote_started_jobs` | job stuck `queued` while its rows are in flight |
| `release_stranded_rows` | `processing` rows past the lease cutoff (wraps the existing sweep) |
| `abort_orphan_rows` | `pending`/`processing` rows under a terminal parent |
| `resync_job_counters` | counter drift on still-active jobs |
| `report_row_count_drift` | read-only: reports `row_count` ≠ rows present |

Each returns a `RepairAction(name, changed, notes)`. `run_repairs()` runs all
six and returns a `RepairReport` with a flat log line list.

### D. Where the automatic repair runs

Inside the **existing stuck-queue watchdog daemon thread** in the worker. It
already opens its own fresh short-lived connection every 30 s, already performs
the lease sweep, and is already deliberately isolated from the shared DB pool so
it cannot wedge with it. Adding a repair pass there is one call site and no new
supervision surface. Cadence is decoupled at
`BULKVID_REPAIR_INTERVAL_SECONDS` (default 120 s) so repairs don't run on every
30 s probe.

The poll path stays **read-only**. Mutating job state as a side effect of the
route the sidebar hits every few seconds is how you get a repair firing
concurrently with an in-progress submit.

### E. Repair audit (`repair_audit` table)

One row per action that changed something, plus the log text. Same shape as the
existing `kill_audit` / `wedge_forensics` tables (opportunistic prune past a
TTL, 30 days). This exists because the entire point of the unattended pass is
that it runs while nobody is watching: without a durable record, a week of
vacation self-heals leaves no evidence of what broke. Surfaced through
`GET /jobs/repair-log` and rendered in the sidebar.

### F. Sidebar + Code.gs

* **"Fix stuck jobs"** button → `POST /jobs/repair` → plain-language result
  ("3 stuck jobs were finished. Nothing was lost."), with the technical log
  behind a *Details* expander. No "finalize" / "PROCESSING" / "reconcile"
  wording in the primary copy.
* **"Self-heal log"** pane → `GET /jobs/repair-log`, showing unattended repairs
  with timestamps.
* **"Restart worker (HuggingFace)"** in both the menu and the sidebar →
  `POST https://huggingface.co/api/spaces/{repo_id}/restart` called **directly
  from Apps Script**, so it works when our backend is the wedged thing. Verified
  2026-08-17 against `huggingface_hub/hf_api.py` (`restart_space`): POST to that
  path with `Authorization: Bearer <token>`, optional `?factory=true`;
  `GET .../runtime` returns the stage. The dialog reports the stage before and
  after so the operator sees it come back.
* A 60 s client-side cooldown in Script Properties, so a lazy user mashing the
  button cannot restart-loop the Space.
* Every addition is additive: an older Code.gs keeps working, and a newer
  Code.gs against an older backend degrades to a clear message rather than an
  exception.

### G. Explicitly rejected

* **No automatic Space restart.** The worker already self-restarts via
  `os._exit` + supervisord `autorestart`, which is the targeted cure. A
  container that restarts itself with no human in the loop can restart-loop
  through a bad deploy, and nobody is watching for several days.
* **No backend-mediated restart endpoint.** It is dead in exactly the scenario
  that motivates the button (web process wedged), and supervisord has no
  control socket configured, so it would also mean new surface in
  `supervisord.conf`.
* **No transactional Hrana batch in this change.** Hrana's `batch` request can
  carry several statements in one round-trip and would restore real atomicity —
  the correct long-term fix for the whole bug class. It is a transport rewrite
  three days after the transport was rolled back once already, and the derived-
  state fix is correct regardless of transport. Filed as the follow-up below.

## Security / safety

* `POST /jobs/repair` is a **write endpoint driven by user input**, so: bearer
  Google-OAuth auth like every other `/jobs/*` route; blast radius scoped to
  `user_email` for bulk users, fleet-wide only for admins (mirrors
  `kill_all_jobs`); every action bounded by SQL predicates, never by a
  client-supplied job id list; no action can touch a `done` row or resurrect a
  terminal job; and the whole call is time-boxed so a flapping DB returns 503,
  not a hung request.
* Repairs only ever move state **toward** terminal, never back into
  processing — except `release_stranded_rows`, which is the existing sweep,
  bounded by a lease cutoff well past the longest row budget.
* The HF token is a **fine-grained token with write scope on that one Space and
  nothing else**, so the worst case if a sheet editor reads it out of Script
  Properties is restarting a Space they can already stop by other means. It is
  never sent to our backend and never logged. Documented in `apps_script/README.md`.
* `GET /jobs/repair-log` returns only actions and counts, never row payloads or
  another user's job ids.
* No new paid service. HF Space restarts are free and do not change billing.

## QA plan

* Unit: counter derivation converges after a simulated partial write (statement
  2 lands, 3 raises, function retried); finalize refuses a job with zero rows
  present; finalize accepts an inflated `row_count` only past the grace age;
  set-based kill aborts N rows in one statement and produces byte-identical
  result JSON to the old per-row path; each repair action is a no-op on a clean
  DB and idempotent when run twice.
* Route: `/jobs/repair` scoping (bulk user cannot repair another user's job),
  503 on `QueueBusy`, and the plain-language summary shape.
* Manual on the live Space: run a real batch, kill it mid-flight (expect no
  504), then hand-corrupt a job to `running` with all rows done and confirm both
  the automatic pass and the button clear it.

## Follow-up (not this PR)

Restore real atomicity by sending each `_tx()` block as ONE Hrana `batch`
request. That removes the partial-write class entirely rather than making each
site idempotent one at a time, and would let `_tx()` mean what it says again.
