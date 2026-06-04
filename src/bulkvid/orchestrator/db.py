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

import sqlite3
from pathlib import Path
from typing import Any

from bulkvid.logging import get_logger

_log = get_logger("db")


# Backend names — surfaced in boot logs so a deploy can be sanity-checked
# at a glance ("did this worker actually pick up the Turso URL?").
BACKEND_SQLITE = "sqlite_local"
BACKEND_LIBSQL_REMOTE = "libsql_remote"
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
        # Translate the three transaction statements queue.py issues via
        # raw ``execute`` into libsql's native commit/rollback. BEGIN is a
        # no-op because libsql implicitly starts a transaction on the
        # first DML in DEFERRED isolation mode (libsql's default).
        if _is_begin_stmt(sql):
            return _LibsqlCursor(_NoopCursor())
        if _is_commit_stmt(sql):
            self._conn.commit()
            return _LibsqlCursor(_NoopCursor())
        if _is_rollback_stmt(sql):
            self._conn.rollback()
            return _LibsqlCursor(_NoopCursor())
        return _LibsqlCursor(self._conn.execute(sql, params))

    def executemany(self, sql: str, params_seq: Any) -> _LibsqlCursor:
        return _LibsqlCursor(self._conn.executemany(sql, params_seq))

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


# ── Public ─────────────────────────────────────────────────────────────────


def connect(
    db_path: Path | str,
    *,
    sync_url: str = "",
    auth_token: str = "",
    sync_interval_seconds: float = 1.0,
    check_same_thread: bool = False,
    timeout: float = 30.0,
) -> Any:
    """Open a DB-API 2.0 connection.

    When ``sync_url`` is empty (the common dev/test path), this is just
    ``sqlite3.connect`` with the same kwargs we've always used. When
    ``sync_url`` is set, we hand off to libsql's embedded-replica mode,
    which keeps a local SQLite file in sync with the remote Turso DB.

    ``auth_token`` is required when ``sync_url`` is set.

    The local replica file lives at ``db_path`` either way, so test code
    that inspects the file (e.g. checking row counts) keeps working.
    """
    path_str = str(db_path)
    Path(path_str).parent.mkdir(parents=True, exist_ok=True)

    if not sync_url:
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

    # Lazy import: the libsql package builds from Rust source on platforms
    # without a pre-built wheel (e.g. Python 3.14 on Windows), and we
    # don't want to force every dev to have a Rust toolchain. The Linux
    # Docker container has the wheel; local devs without it stay on
    # sqlite3 mode by leaving BULKVID_DB_URL empty.
    import libsql    # type: ignore[import-not-found]

    _log.info(
        "db_backend",
        backend=BACKEND_LIBSQL_REMOTE,
        path=path_str,
        sync_url=_redact_host(sync_url),
    )
    # Remote mode: every statement is an HTTPS round-trip to Turso. No
    # local file is touched — ``path_str`` is accepted for API symmetry
    # with the sqlite3 path (callers like JobQueue compute the path for
    # logging) but isn't passed to libsql. Multi-statement transactions
    # are still atomic: libsql buffers them until ``commit()``, which
    # our wrapper translates from ``execute("COMMIT")``.
    raw = libsql.connect(sync_url, auth_token=auth_token)
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
