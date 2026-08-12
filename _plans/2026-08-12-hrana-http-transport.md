# Hrana-over-HTTP DB transport (the root cure, twice deferred)

Date: 2026-08-12
Branch: `db-hrana-http-transport` (PR 2 of 2)
Status: approved by Yoav (2026-08-12) as part of the two-PR plan from the
5-advisor council pass. PR 1 (`_plans/2026-08-12-worker-liveness-net-heartbeat-forensics.md`)
is merged and deployed.

## The problem, in one sentence

Every "the Space is stuck, restart it" incident traces to two properties of the
sync `libsql` client, neither of which is a property of Turso itself: a stalled
statement blocks its thread **forever and uncancellably**, and a long-lived
logical session can serve a **stale read snapshot with no error raised**.

## Why this is the root cure and the watchdogs were not

A call that can hang forever has an *unbounded* failure space. Five watchdogs
enumerate five shapes; an unbounded space always has a sixth, which is exactly
the history here: six incident plans since June, each patching the previous
incident's signature while the next wedge found the seam between guards. The
2026-08-12 freeze (6 rows frozen mid-flight) was the sixth shape.

This PR shrinks the failure space instead of patrolling it:

- **Real deadlines.** Every statement carries a hard connect/read timeout, so a
  stall RAISES instead of parking a thread. That converts the unbounded class
  into an ordinary exception `JobQueue._run_db` already retries and reconnects
  through. No leaked pool threads means no wedged pool.
- **No session to go stale.** Every call sends `baton: null` and a trailing
  `close`, so the server opens a fresh stream, reads the latest committed state,
  and drops it inside one round-trip. There is no snapshot that can lag.

The estimate that got this deferred twice ("weeks of work, high risk") priced a
full async rewrite. This is not that: the DB-API shim, the autocommit semantics,
and the `_DictRow` row type all stay exactly as they are.

## Chosen approach

**`orchestrator/hrana.py` (new)** — the wire protocol only, no DB-API concepts:
`HranaClient.execute` / `.execute_script` / `.close`, value encode/decode, URL
normalisation, and the two error types (`HranaError` for SQL-level failures,
`HranaTransportError` for timeouts/HTTP/malformed bodies). Built on `httpx`,
already a base dependency.

**`orchestrator/db.py`** — `_HranaConn` + `_HranaCursor` present the identical
DB-API surface to `_LibsqlConn`: same `_DictRow` rows, same BEGIN/COMMIT/ROLLBACK
no-op translation, same autocommit. No call site can tell which transport it is
talking to except by the good difference.

Protocol details verified 2026-08-12 against the Hrana 3 spec and Turso's "SQL
over HTTP" reference (Context7 was not reachable this session, so the official
docs were used directly per rule 1):

- Integers travel as **strings** (64-bit precision through JSON), floats as
  numbers, blobs under a **`base64`** key rather than `value`.
- `POST /v2/pipeline`; HTTP 4xx/5xx is a transport failure, while a per-statement
  SQL failure returns inside a 200 as `{"type": "error"}` in `results`.
- `executescript` maps to the protocol's own `sequence` request, so a `;` inside
  a string literal can never be mis-split by us.

### Connection pooling is kept, sessions are not

`httpx.Client` reuses TCP/TLS across calls. That is a transport optimisation and
is unrelated to the *logical* session that caused stale reads, so it costs
nothing in correctness and saves a handshake per statement.

## Rollout: opt-in, with a probe that makes the flip safe

`BULKVID_DB_TRANSPORT` selects the transport. **Default stays `libsql`**, so
merging and deploying this PR changes nothing in production until the variable
is set to `hrana` in the Space settings.

Default-off is deliberate. One integration point cannot be verified from a dev
machine without production Turso credentials: `PRAGMA journal_mode=WAL`, which
`queue.py` and `settings_store.py` send via `executescript` at every connection
open. It is almost certainly accepted (Turso runs libsql server-side) but "almost
certainly" is not verification.

So `connect()` **probes before committing**: it opens the Hrana client, runs
`SELECT 1`, and only returns it on success. Any failure logs `hrana_probe_failed`
at ERROR and falls through to the libsql client. Without that probe, an
incompatibility would crash-loop BOTH supervisord processes and take the service
down until someone unset the variable by hand; with it, the worst case is "we
logged an error and kept using the transport that already works", which is
strictly no worse than today. Cost is one extra round-trip per connection open
(boot and reconnect), not per query.

`/health/deep` reports both `db.transport` (configured) and `db.live_backend`
(what the connection actually became). They diverge exactly when the probe fell
back, which is the one case where reading the env var alone would mislead.

## Security & safety (rule 13)

- No new external surface: the same Turso host, the same auth token, over the
  same TLS. The token travels in an `Authorization: Bearer` header instead of
  inside the client library, and is never logged (`_redact_host` already trims
  URLs to a bare host for logs).
- Bounded by construction: connect, read, write and pool waits all have
  deadlines, so no call path can block indefinitely. That is a security property
  as much as a reliability one, since an unbounded wait is a denial-of-service
  against ourselves.
- Fails safe everywhere: probe failure falls back, SQL errors raise a distinct
  type from transport errors (so the retry machinery does not hammer a
  deterministic failure), and unsupported parameter types are rejected before
  anything is sent.
- No change to what is stored or who can read it.

## Observability (rule 14)

- `db_backend backend=hrana_http` at connection open (suppressed under `quiet`
  for the watchdog's 30s probes, as with the other backends).
- `hrana_probe_failed` (ERROR) with the error type, plus `hrana_client_init_failed`.
- `/health/deep`: `db.transport` and `db.live_backend`.

## Settings audit (rule 15)

Env vars, consistent with every other transport knob:

- `BULKVID_DB_TRANSPORT` (`libsql` default, `hrana` opt-in; unknown values fall
  back rather than failing a deploy)
- `BULKVID_HRANA_CONNECT_TIMEOUT_SECONDS` (default 5)
- `BULKVID_HRANA_READ_TIMEOUT_SECONDS` (default 10)

The read budget sits deliberately BELOW `queue._DB_CALL_TIMEOUT_SECONDS` (15s)
so a stall surfaces as a clean exception from the transport, which `_run_db`
retries on a fresh connection, rather than the outer `wait_for` abandoning a call
still running underneath.

No cost implication: same database, same plan, and per-statement HTTP requests
are what the libsql client was already making under the hood.

## Testing (rule 18)

`tests/unit/test_hrana.py` (new, respx-backed so the real `httpx.Client` and its
timeout plumbing are exercised): request bodies asserted literally
(`baton: null` + trailing `close`, `want_rows`, auth header); every parameter
type encoded correctly including `bool` before `int` and blob-as-base64; named
args; `sequence` for scripts; every value type decoded including a 64-bit integer
beyond float53 precision; `affected_row_count` and string `last_insert_rowid`;
and the whole failure surface (read timeout, connect error, HTTP 4xx, SQL error
inside a 200, non-JSON body, missing results array, unbindable parameter).

`tests/unit/test_db_adapter.py`: transport selection and garbage-value fallback;
`_DictRow` parity by name and index; BEGIN/COMMIT/ROLLBACK never reaching the
wire; `rowcount`/`lastrowid`; fetch semantics; `executescript` via sequence;
`executemany` unrolling; `row_factory` assignment tolerated; close propagation;
probe success returning `_HranaConn`; **probe failure falling back to libsql**.

`tests/unit/test_routes_health.py`: `transport` vs `live_backend` divergence.

Full `tests/unit` suite green. `mypy src` error count unchanged at 97 despite a
new file, so this adds no type errors; `ruff` adds only the codebase's existing
`# noqa: BLE001` convention.

## Deploy (rule 19)

1. Merge and deploy normally (`git push hf main`). **No behaviour change** —
   default is still `libsql`.
2. Set `BULKVID_DB_TRANSPORT=hrana` in the Space settings. The Space restarts.
3. Confirm on `/health/deep` that `db.live_backend` is `hrana_http`. If it says
   `libsql_remote`, the probe fell back: check the logs for `hrana_probe_failed`.
4. Burn in for 48h with PR 1's liveness net armed underneath.
5. Rollback at any point: unset the variable (no code change, no redeploy).

## Follow-up once burned in

- Delete the libsql client path and the `turso` optional dependency. Do not keep
  both transports alive longer than a week or we will maintain both forever.
- Retire the now-redundant shape-specific guards into PR 1's invariant-based net.
- Revisit `_DB_EXECUTOR_THREADS`: with deadlines enforced, threads can no longer
  leak, so the pool exists only for concurrency rather than for wedge headroom.

## Documented limitations

- Interactive multi-statement transactions remain unavailable. Unchanged from
  today: remote mode has run in autocommit since 2026-06-04 and the shim already
  no-ops BEGIN/COMMIT.
- The probe adds one round-trip per connection open.
- `PRAGMA journal_mode=WAL` over `sequence` is the single call not verified
  against production Turso; the probe plus default-off exist precisely to make
  that unverified case harmless.
