"""Database backend selector — sqlite3 locally, libSQL/Turso in prod.

The queue and the settings store both want the SAME small slice of the
DB-API 2.0 surface: ``execute``, ``executemany``, ``executescript``,
``cursor``, dict-like row access (``row["col_name"]`` à la
``sqlite3.Row``), ``BEGIN IMMEDIATE`` via ``execute``, ``commit``,
``rollback``, ``close``. We pick the backend at runtime by URL:

  - ``BULKVID_DB_URL`` empty  → plain ``sqlite3.connect(db_path, ...)``
    (current behaviour: local dev, the test suite, anywhere we don't
    need cloud persistence).
  - ``BULKVID_DB_URL`` set    → ``libsql.connect(url, auth_token=...)``
    in REMOTE mode: every statement is an HTTPS round-trip to Turso.
    Multi-statement transactions are still atomic because libsql
    buffers between ``execute`` calls and flushes on ``commit()``.

History note: an earlier deploy used libsql's embedded-replica mode
(local SQLite file synced to Turso every N seconds). It died
immediately on HuggingFace Spaces because both web and worker
processes shared the same local file path inside the container; their
WAL replicas corrupted each other and every query raised
``ValueError: file is not a database``. Remote mode sidesteps this by
having no local file at all.

The libsql Python package implements DB-API 2.0 but does NOT support
``connection.row_factory = sqlite3.Row`` (the assignment raises
AttributeError as of libsql 0.1.x). Its cursors return plain tuples.
It also reacts badly to raw ``execute("COMMIT")`` because it manages
transactions internally. Our queue + settings-store code is full of
``row["col_name"]`` access AND uses the sqlite3
autocommit-with-explicit-BEGIN/COMMIT idiom, so we transparently wrap
the libsql connection in a small ``_LibsqlConn`` shim that:

  1. Hands back ``_DictRow`` objects from every ``fetch*`` — same
     surface area as ``sqlite3.Row``, no caller changes.
  2. Translates ``execute("BEGIN ...")`` to a no-op, ``execute("COMMIT")``
     to ``conn.commit()``, and ``execute("ROLLBACK")`` to
     ``conn.rollback()``.

The sqlite3 path stays unwrapped because sqlite3.Row already gives us
name-and-index access natively and the explicit-BEGIN/COMMIT idiom is
what sqlite3 expects in the first place.

Plan: ``_plans/2026-06-04-migrate-to-hf-spaces-turso.md``.
"""

from __future__ import annotations

import asyncio
import functools
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import Any

from bulkvid.logging import get_logger
from bulkvid.orchestrator import hrana as _hrana

_log = get_logger("db")


# ── Dedicated DB thread pool ─────────────────────────────────────────────────
#
# Every remote-libsql statement is a blocking HTTPS round-trip run via a worker
# thread. When Turso flaps, that call can wedge UNCANCELLABLY inside the libsql
# client: ``asyncio.wait_for`` abandons the awaiting coroutine at its timeout,
# but the underlying thread keeps running and is never reclaimed. A single
# failed ``_run_db`` cycle (time-box + reconnect, several attempts) can leak a
# handful of such threads.
#
# If those DB calls run on the *default* asyncio executor (sized
# ``min(32, cpu+4)`` ≈ 6 on a 2-vCPU box — the pool ALSO used by CPU/render
# ``to_thread`` work), a burst of leaked DB threads exhausts it and every later
# ``to_thread`` — DB claims, Pillow renders, everything — queues forever. That
# is the "stuck 30+ min, nothing happens" wedge (Plan
# ``_plans/2026-07-06-stuck-runs-worker-wedge.md``; libsql-uncancellable-thread
# root cause per the council review).
#
# Routing ALL DB work through a dedicated, larger, bounded pool (a) isolates DB
# I/O from CPU/render so a Turso flap can never starve rendering (and vice
# versa), and (b) gives many failed-flap cycles of headroom before the DB pool
# itself exhausts — at which point ``claim_next_row`` fails cleanly and the
# runner's liveness watchdog can act. It does NOT stop the leak (only a
# transport-level deadline / async libsql client does that — the deferred real
# fix); it contains the blast radius. Size is memory-neutral: concurrent
# per-row MP4 buffering is capped by the runner's row semaphore, not by DB
# thread count. Env-tunable for a per-deploy tune without a code change.
_DB_EXECUTOR_DEFAULT_THREADS = 16
_db_executor: ThreadPoolExecutor | None = None


def _db_executor_size() -> int:
    raw = os.environ.get("BULKVID_DB_EXECUTOR_THREADS")
    if raw:
        try:
            v = int(raw)
            if v >= 1:
                return v
        except ValueError:
            pass
    return _DB_EXECUTOR_DEFAULT_THREADS


def get_db_executor() -> ThreadPoolExecutor:
    """Return the process-wide dedicated DB thread pool, creating it on first use.

    Lazily created so importing this module has no side effects. Called from a
    single event loop per process (web OR worker), so the check-then-assign
    below has no ``await`` and cannot race."""
    global _db_executor
    if _db_executor is None:
        size = _db_executor_size()
        _db_executor = ThreadPoolExecutor(
            max_workers=size, thread_name_prefix="bulkvid-db"
        )
        _log.info("db_executor_init", max_workers=size)
    return _db_executor


# ── DB-pool wedge tracking (Plan 2026-07-07 §Phase 1) ────────────────────────
#
# The dedicated pool above isolates DB I/O, but it does NOT stop the leak: a
# libsql call that wedges on a dead socket is uncancellable, so its thread never
# returns and ``asyncio.wait_for`` only abandons the awaiting coroutine. Enough
# leaked threads (~pool_size) and the pool is 100% dead — the web submit hang /
# worker stall that only a container restart fixes.
#
# We can't cancel the thread, so we make the wedge OBSERVABLE and let a plain
# watchdog thread + supervisord ``autorestart`` recover (``db_watchdog.py``).
# Each executor body records its start in ``_inflight`` on entry and clears it on
# exit. Only *running* bodies hold an entry (queued-but-unstarted submissions
# don't), so ``_inflight`` is bounded by ``pool_size`` and never grows unbounded.
# A body still present after ``_DB_WEDGE_SECONDS`` is almost certainly wedged (a
# healthy remote statement returns in well under a second). ``db_pool_stats``
# reports how many threads are wedged right now.
#
# This is deliberately NOT the "swap in a fresh pool" self-healer the council
# rejected: abandoned pools still pin their wedged threads/sockets and, under a
# sustained flap, unbounded pool churn OOMs a small box. Detection + a clean
# process restart is bounded and safe. The real cure (a transport-level deadline
# via the async libsql/hrana client) is Phase 2.
_DB_WEDGE_SECONDS = float(os.environ.get("BULKVID_DB_WEDGE_SECONDS") or 60.0)

_inflight_lock = threading.Lock()
_inflight: dict[int, float] = {}    # call_id -> start (time.monotonic)
_call_seq = 0


def _next_call_id() -> int:
    global _call_seq
    with _inflight_lock:
        _call_seq += 1
        return _call_seq


def _tracked_call[**P, R](
    call_id: int, fn: Callable[P, R], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> R:
    """Executor body: record start/finish around ``fn`` so a wedged (never-
    returning) call leaves a durable entry the watchdog can see. Runs INSIDE the
    DB thread. The ``finally`` clears the entry on any normal return/raise; only
    a thread wedged forever keeps its entry, which is exactly the signal we
    want."""
    start = time.monotonic()
    with _inflight_lock:
        _inflight[call_id] = start
    try:
        return fn(*args, **kwargs)
    finally:
        with _inflight_lock:
            _inflight.pop(call_id, None)


def db_pool_stats() -> dict[str, int]:
    """Snapshot the dedicated DB pool's live health for the watchdog + logs.

    ``running`` = executor bodies currently in ``fn`` (≤ ``pool_size``).
    ``wedged``  = of those, how many have been running longer than
    ``_DB_WEDGE_SECONDS`` (presumed stuck on a dead libsql socket).
    ``pool_size`` = configured max threads. When ``wedged == pool_size`` the pool
    can no longer serve any DB call — the wedge state the watchdog restarts on."""
    now = time.monotonic()
    with _inflight_lock:
        starts = list(_inflight.values())
    wedged = sum(1 for s in starts if (now - s) >= _DB_WEDGE_SECONDS)
    return {
        "pool_size": _db_executor_size(),
        "running": len(starts),
        "wedged": wedged,
    }


async def run_db_call[**P, R](
    fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs
) -> R:
    """Run a blocking DB helper on the dedicated DB pool (not the default one).

    Drop-in for ``asyncio.to_thread(fn, *args, **kwargs)`` — same signature and
    inference (``ParamSpec`` ties the args to ``fn``) — that keeps DB I/O off
    the shared CPU/render executor. Wrapped in ``_tracked_call`` so the DB-pool
    wedge watchdog can see stuck threads. ``functools.partial`` carries the
    kwargs because ``run_in_executor`` takes only positional args."""
    loop = asyncio.get_running_loop()
    call_id = _next_call_id()
    return await loop.run_in_executor(
        get_db_executor(),
        functools.partial(_tracked_call, call_id, fn, args, kwargs),
    )


# Backend names — surfaced in boot logs so a deploy can be sanity-checked
# at a glance ("did this worker actually pick up the Turso URL?").
BACKEND_SQLITE = "sqlite_local"
BACKEND_LIBSQL_REMOTE = "libsql_remote"
# Stateless SQL-over-HTTP against the same Turso database. Same data, same
# schema, same autocommit semantics as ``libsql_remote`` — it differs only in
# having enforceable per-request deadlines and no long-lived session to serve a
# stale snapshot. Plan ``_plans/2026-08-12-hrana-http-transport.md``.
BACKEND_HRANA_HTTP = "hrana_http"
# Kept for backwards-compat with any external grep / docs / older plan
# references. The 14:13 deploy on 2026-06-04 proved embedded-replica mode
# is unsafe when two processes (web + worker) share a single container's
# local file path: the WAL replicas corrupted each other and every query
# died with ``ValueError: file is not a database``. We use remote mode
# instead — every statement is one HTTPS round-trip to Turso, no shared
# local file. Trade ~10-30 ms per query for correctness.
BACKEND_LIBSQL_REPLICA = "libsql_embedded_replica"    # historical; no longer selected


# ── libsql tuple-to-dict shims ──────────────────────────────────────────────


class _DictRow:
    """``sqlite3.Row``-compatible row backed by a (tuple, column_names) pair.

    Implements just the surface the queue + settings store actually use:
    ``row["col"]`` (by name), ``row[i]`` (by index), ``row.keys()``, and
    iteration. Lets every caller that does
    ``Job(**{k: row[k] for k in row.keys()})`` keep working unchanged when
    the underlying driver is libsql (which returns plain tuples).
    """

    __slots__ = ("_data", "_keys")

    def __init__(self, data: tuple[Any, ...], keys: tuple[str, ...]) -> None:
        self._data = data
        self._keys = keys

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return self._data[key]
        if isinstance(key, str):
            try:
                idx = self._keys.index(key)
            except ValueError as e:
                raise IndexError(f"no column named {key!r}") from e
            return self._data[idx]
        raise TypeError(
            f"row indices must be int or str, got {type(key).__name__}"
        )

    def keys(self) -> list[str]:
        return list(self._keys)

    def __iter__(self) -> Any:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:    # debug aid only; not on a hot path
        return f"_DictRow({dict(zip(self._keys, self._data, strict=False))!r})"


class _NoopCursor:
    """Stand-in cursor returned from translated BEGIN/COMMIT/ROLLBACK calls.

    queue.py and settings_store.py never read the return value of those
    statements, so this just needs to be safely callable for the
    attributes ``_LibsqlCursor`` forwards. Keeps the wrapper from blowing
    up if a future caller does ``cur.fetchone()`` on the result of a
    transaction statement.
    """

    description: list[tuple] | None = None
    rowcount: int = 0
    lastrowid: int | None = None

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list:
        return []

    def fetchmany(self, size: int | None = None) -> list:
        return []

    def __iter__(self) -> Any:
        return iter(())

    def close(self) -> None:
        return None


class _LibsqlCursor:
    """Thin pass-through cursor that wraps every fetched row in ``_DictRow``.

    We only override the ``fetch*`` family. Everything else (``description``,
    ``rowcount``, ``lastrowid``, ``close``, iteration) forwards to the
    underlying libsql cursor via ``__getattr__``.
    """

    def __init__(self, cur: Any) -> None:
        self._cur = cur

    def _column_names(self) -> tuple[str, ...]:
        desc = self._cur.description
        return tuple(c[0] for c in desc) if desc else ()

    def fetchone(self) -> _DictRow | None:
        row = self._cur.fetchone()
        if row is None:
            return None
        return _DictRow(tuple(row), self._column_names())

    def fetchall(self) -> list[_DictRow]:
        keys = self._column_names()
        return [_DictRow(tuple(r), keys) for r in self._cur.fetchall()]

    def fetchmany(self, size: int | None = None) -> list[_DictRow]:
        keys = self._column_names()
        rows = self._cur.fetchmany(size) if size is not None else self._cur.fetchmany()
        return [_DictRow(tuple(r), keys) for r in rows]

    def __iter__(self) -> Any:
        keys = self._column_names()
        for row in self._cur:
            yield _DictRow(tuple(row), keys)

    def __getattr__(self, name: str) -> Any:
        # Forward anything we haven't explicitly overridden — description,
        # rowcount, lastrowid, close, arraysize, etc.
        return getattr(self._cur, name)


def _is_begin_stmt(sql: str) -> bool:
    """``BEGIN`` / ``BEGIN IMMEDIATE`` / ``BEGIN EXCLUSIVE`` / ``BEGIN DEFERRED``."""
    head = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
    return head == "BEGIN"


def _is_commit_stmt(sql: str) -> bool:
    head = sql.strip().rstrip(";").upper()
    return head in ("COMMIT", "END", "COMMIT TRANSACTION", "END TRANSACTION")


def _is_rollback_stmt(sql: str) -> bool:
    head = sql.strip().rstrip(";").upper()
    return head in ("ROLLBACK", "ROLLBACK TRANSACTION")


class _LibsqlConn:
    """Connection wrapper that returns ``_LibsqlCursor`` from every
    ``execute``/``executemany``/``cursor`` call so callers see dict-like
    rows. Also translates raw transaction statements into libsql's native
    transaction methods — ``execute("BEGIN IMMEDIATE")`` becomes a no-op,
    ``execute("COMMIT")`` becomes ``conn.commit()``, and
    ``execute("ROLLBACK")`` becomes ``conn.rollback()``.

    Why: libsql manages WAL transactions internally (via its
    ``commit()`` / ``rollback()`` methods). If you hand it a raw
    ``execute("COMMIT")`` it tries to start a fresh WAL transaction to
    "commit", which then fails with ``ValueError: wal_insert_begin
    failed``. queue.py's ``_tx()`` context manager was written for plain
    sqlite3's autocommit-with-explicit-BEGIN/COMMIT idiom; this shim
    keeps that idiom working unchanged for libsql callers.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        # Stored but ignored — every cursor we return already provides
        # name-and-index access. Lets caller code keep its
        # ``conn.row_factory = sqlite3.Row`` line without an exception.
        self.row_factory: Any = None

    def execute(self, sql: str, params: Any = ()) -> _LibsqlCursor:
        # In autocommit mode (set in db.connect via isolation_level=None)
        # every statement is its own transaction. BEGIN / COMMIT /
        # ROLLBACK become pure no-ops — we mustn't forward them to
        # libsql at all, because libsql's behaviour on a redundant
        # commit/rollback when there's nothing to commit varies and
        # we've seen it cause stale-read symptoms in remote mode. Drop
        # them on the floor; the SQL behind ``with _tx():`` blocks is
        # already idempotent enough thanks to idempotency keys + the
        # recover_orphaned_rows boot pass (queue.py was designed to
        # tolerate partial writes).
        if _is_begin_stmt(sql) or _is_commit_stmt(sql) or _is_rollback_stmt(sql):
            return _LibsqlCursor(_NoopCursor())
        return _LibsqlCursor(self._conn.execute(sql, params))

    def executemany(self, sql: str, params_seq: Any) -> _LibsqlCursor:
        # libsql's remote-mode executemany has been observed to silently
        # no-op on our INSERT INTO row_queue path (jobs row lands, row_queue
        # rows don't — the worker's JOIN then sees zero pending rows and
        # the queue is permanently stuck). Defensive: iterate and use plain
        # execute, which is verified to work in remote mode. The cost is
        # one extra HTTPS round-trip per row, which is irrelevant for our
        # batch sizes (a 50-row submit becomes 50 round-trips ≈ 2-3
        # seconds, negligible against the multi-minute pipeline).
        last_cur: Any = _NoopCursor()
        for params in params_seq:
            last_cur = self._conn.execute(sql, params)
        return _LibsqlCursor(last_cur)

    def executescript(self, sql: str) -> Any:
        # Return whatever libsql returns — callers never read this cursor.
        return self._conn.executescript(sql)

    def cursor(self) -> _LibsqlCursor:
        return _LibsqlCursor(self._conn.cursor())

    def commit(self) -> Any:
        return self._conn.commit()

    def rollback(self) -> Any:
        return self._conn.rollback()

    def close(self) -> Any:
        return self._conn.close()

    def sync(self) -> Any:
        # Embedded-replica only; remote-mode connections lack this method,
        # so guard the attribute lookup.
        sync_fn = getattr(self._conn, "sync", None)
        if sync_fn is None:
            return None
        return sync_fn()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _HranaCursor:
    """DB-API cursor over an already-materialised ``StatementResult``.

    The HTTP protocol has no streaming cursor: one round-trip returns the whole
    result set. So this is a cursor over a list, which keeps ``fetchone`` /
    ``fetchall`` / iteration working exactly as callers expect while doing no
    further I/O. Rows are handed back as ``_DictRow`` — the same type the libsql
    path returns — so no call site can tell the transports apart.
    """

    def __init__(self, result: _hrana.StatementResult) -> None:
        self._result = result
        self._pos = 0

    @property
    def description(self) -> list[tuple[Any, ...]] | None:
        """Only the column-name slot is populated; that is all
        ``_DictRow``-based callers ever read."""
        if not self._result.cols:
            return None
        return [(name, None, None, None, None, None, None)
                for name in self._result.cols]

    @property
    def rowcount(self) -> int:
        return self._result.affected_row_count

    @property
    def lastrowid(self) -> int | None:
        return self._result.last_insert_rowid

    def fetchone(self) -> _DictRow | None:
        if self._pos >= len(self._result.rows):
            return None
        row = self._result.rows[self._pos]
        self._pos += 1
        return _DictRow(row, self._result.cols)

    def fetchall(self) -> list[_DictRow]:
        rows = self._result.rows[self._pos:]
        self._pos = len(self._result.rows)
        return [_DictRow(r, self._result.cols) for r in rows]

    def fetchmany(self, size: int | None = None) -> list[_DictRow]:
        n = len(self._result.rows) - self._pos if size is None else max(size, 0)
        rows = self._result.rows[self._pos:self._pos + n]
        self._pos += len(rows)
        return [_DictRow(r, self._result.cols) for r in rows]

    def __iter__(self) -> Any:
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def close(self) -> None:
        return None


class _HranaConn:
    """Connection wrapper presenting the same DB-API surface as ``_LibsqlConn``,
    backed by stateless SQL-over-HTTP instead of the sync libsql client.

    Behavioural parity is the whole design goal: identical ``_DictRow`` rows,
    identical BEGIN/COMMIT/ROLLBACK no-op translation, identical autocommit
    semantics. The ONLY difference a caller can observe is the good one — a
    stalled statement now raises on a deadline instead of blocking its thread
    forever. Plan ``_plans/2026-08-12-hrana-http-transport.md``.
    """

    def __init__(self, client: _hrana.HranaClient) -> None:
        self._client = client
        # Stored but ignored, exactly as in ``_LibsqlConn``: every cursor we
        # return already provides name-and-index access, and callers assign
        # ``conn.row_factory = sqlite3.Row`` unconditionally.
        self.row_factory: Any = None

    def execute(self, sql: str, params: Any = ()) -> Any:
        # Same translation as the libsql path: in autocommit every statement is
        # its own transaction, so transaction-control statements are dropped on
        # the floor rather than sent. Forwarding them would be worse here than
        # for libsql — a stateless request cannot hold a transaction open at
        # all, so a real BEGIN would silently do nothing anyway.
        if _is_begin_stmt(sql) or _is_commit_stmt(sql) or _is_rollback_stmt(sql):
            return _NoopCursor()
        return _HranaCursor(self._client.execute(sql, params))

    def executemany(self, sql: str, params_seq: Any) -> Any:
        # One request per parameter set. Matches the libsql shim, which also
        # unrolls executemany after remote-mode batching was observed to
        # silently no-op our row_queue inserts (see ``_LibsqlConn``).
        last: Any = None
        for params in params_seq:
            last = self._client.execute(sql, params)
        return _NoopCursor() if last is None else _HranaCursor(last)

    def executescript(self, sql: str) -> Any:
        self._client.execute_script(sql)
        return _NoopCursor()

    def cursor(self) -> Any:
        # Intentionally unsupported: no call site in this codebase uses a
        # standalone cursor, and returning an empty one would fail silently.
        raise NotImplementedError(
            "the Hrana transport has no standalone cursor; use conn.execute()"
        )

    def commit(self) -> Any:
        return None    # autocommit — every statement already committed

    def rollback(self) -> Any:
        return None    # nothing is ever left open to roll back

    def close(self) -> Any:
        return self._client.close()

    def sync(self) -> Any:
        return None    # embedded-replica concept; meaningless over HTTP


# ── Public ─────────────────────────────────────────────────────────────────


TRANSPORT_LIBSQL = "libsql"
TRANSPORT_HRANA = "hrana"


def db_transport() -> str:
    """Which remote transport this process should use.

    Defaults to the long-serving ``libsql`` client. Set
    ``BULKVID_DB_TRANSPORT=hrana`` to select the HTTP transport with real
    per-request deadlines; any unrecognised value falls back to the default
    rather than failing a deploy. Surfaced in ``/health/deep`` so the active
    choice is verifiable without reading env vars off the box."""
    raw = (os.environ.get("BULKVID_DB_TRANSPORT") or "").strip().lower()
    return raw if raw in (TRANSPORT_LIBSQL, TRANSPORT_HRANA) else TRANSPORT_LIBSQL


def _connect_hrana_checked(
    sync_url: str, auth_token: str, *, quiet: bool
) -> Any | None:
    """Open a Hrana connection and prove it works, or return ``None``.

    The probe is what makes ``BULKVID_DB_TRANSPORT=hrana`` safe to flip on a
    live Space. Without it, any incompatibility (a rejected statement shape, an
    auth quirk, a proxy in front of the endpoint) would crash-loop BOTH
    processes under supervisord and take the service down until someone unset
    the variable by hand. With it, the worst case degrades to "we logged an
    error and kept using the transport that already works" — strictly no worse
    than today. One extra round-trip per connection open, which happens at boot
    and on reconnect, not per query."""
    try:
        client = _hrana.HranaClient(sync_url, auth_token)
    except Exception as e:    # noqa: BLE001 — never let transport choice crash boot
        _log.error("hrana_client_init_failed", error=str(e)[:200])
        return None
    try:
        client.execute("SELECT 1")
    except Exception as e:    # noqa: BLE001 — fall back rather than crash-loop
        _log.error(
            "hrana_probe_failed",
            error=str(e)[:200],
            error_type=type(e).__name__,
            note="falling back to the libsql client for this connection",
        )
        with suppress(Exception):
            client.close()
        return None
    if not quiet:
        _log.info(
            "db_backend",
            backend=BACKEND_HRANA_HTTP,
            sync_url=_redact_host(sync_url),
        )
    return _HranaConn(client)


def connect(
    db_path: Path | str,
    *,
    sync_url: str = "",
    auth_token: str = "",
    sync_interval_seconds: float = 1.0,
    check_same_thread: bool = False,
    timeout: float = 30.0,
    quiet: bool = False,
) -> Any:
    """Open a DB-API 2.0 connection.

    When ``sync_url`` is empty (the common dev/test path), this is just
    ``sqlite3.connect`` with the same kwargs we've always used. When
    ``sync_url`` is set, we hand off to libsql's embedded-replica mode,
    which keeps a local SQLite file in sync with the remote Turso DB.

    ``auth_token`` is required when ``sync_url`` is set.

    The local replica file lives at ``db_path`` either way, so test code
    that inspects the file (e.g. checking row counts) keeps working.

    ``quiet`` suppresses the one-line ``db_backend`` INFO log — for callers that
    open a fresh connection on a tight cadence (the stuck-queue watchdog probes
    every ~30s) and would otherwise flood the very logs used to diagnose a wedge.
    """
    path_str = str(db_path)
    Path(path_str).parent.mkdir(parents=True, exist_ok=True)

    if not sync_url:
        if not quiet:
            _log.info("db_backend", backend=BACKEND_SQLITE, path=path_str)
        return sqlite3.connect(
            path_str,
            check_same_thread=check_same_thread,
            timeout=timeout,
            isolation_level=None,
        )

    if not auth_token:
        raise ValueError(
            "BULKVID_DB_URL is set but BULKVID_DB_AUTH_TOKEN is empty — "
            "libsql embedded replica requires both."
        )

    # Opt-in HTTP transport with real per-request deadlines. Probed before use;
    # a failed probe logs loudly and falls through to the libsql client below,
    # so flipping this env var can never take the service down.
    if db_transport() == TRANSPORT_HRANA:
        conn = _connect_hrana_checked(sync_url, auth_token, quiet=quiet)
        if conn is not None:
            return conn

    # Lazy import: the libsql package builds from Rust source on platforms
    # without a pre-built wheel (e.g. Python 3.14 on Windows), and we
    # don't want to force every dev to have a Rust toolchain. The Linux
    # Docker container has the wheel; local devs without it stay on
    # sqlite3 mode by leaving BULKVID_DB_URL empty.
    import libsql  # type: ignore[import-not-found]

    if not quiet:
        _log.info(
            "db_backend",
            backend=BACKEND_LIBSQL_REMOTE,
            path=path_str,
            sync_url=_redact_host(sync_url),
        )
    # Remote mode: every statement is an HTTPS round-trip to Turso. No
    # local file is touched — ``path_str`` is accepted for API symmetry
    # with the sqlite3 path (callers like JobQueue compute the path for
    # logging) but isn't passed to libsql.
    #
    # ``isolation_level=None`` forces autocommit: every statement is its
    # own transaction, no implicit session-level transaction can hold a
    # stale snapshot. We observed a multi-minute lag between
    # ``INSERT INTO row_queue`` on the web app and the worker's
    # subsequent ``SELECT`` seeing it; that's the classic symptom of
    # libsql holding a logical session and serving stale reads from it.
    # Autocommit removes the session.
    #
    # The atomicity trade-off (no multi-statement transactions): mostly
    # covered by our idempotency-key column and the
    # recover_orphaned_rows boot pass; the queue is already designed to
    # tolerate partial writes.
    raw = libsql.connect(
        sync_url, auth_token=auth_token, isolation_level=None
    )
    # Wrap so callers get sqlite3.Row-compatible dict-rows from every
    # fetch* and so that ``execute("BEGIN"/"COMMIT"/"ROLLBACK")`` gets
    # translated to libsql's native commit/rollback semantics.
    return _LibsqlConn(raw)


def ping(conn: Any) -> float:
    """Round-trip ``SELECT 1`` against the connection and return elapsed ms.

    Used by ``/health/deep`` so admins can see DB latency from the browser
    without SSH. Works for both sqlite3 and libsql connections.
    """
    import time

    started = time.monotonic()
    cur = conn.execute("SELECT 1")
    _ = cur.fetchone()
    return (time.monotonic() - started) * 1000.0


def _redact_host(url: str) -> str:
    """Trim a libsql:// URL down to ``host`` so the auth-token portion (if a
    caller ever sticks one in the URL) and any query string never land in
    a log line."""
    s = url.split("://", 1)[-1]
    s = s.split("/", 1)[0]
    s = s.split("?", 1)[0]
    s = s.split("@", 1)[-1]
    return s
