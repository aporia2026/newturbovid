"""Database backend selector — sqlite3 locally, libSQL/Turso in prod.

The queue and the settings store both want the SAME small slice of the
DB-API 2.0 surface: ``execute``, ``executemany``, ``executescript``,
``cursor``, ``row_factory = sqlite3.Row``, ``BEGIN IMMEDIATE`` via
``execute``, ``commit``, ``rollback``, ``close``. We pick the backend at
runtime by URL:

  - ``BULKVID_DB_URL`` empty  → plain ``sqlite3.connect(db_path, ...)``
    (current behaviour: local dev, the 560-test suite, anywhere we don't
    need cloud persistence).
  - ``BULKVID_DB_URL`` set    → ``libsql.connect(db_path, sync_url=...,
    auth_token=..., sync_interval=...)`` — embedded replica mode. Reads
    are local-SQLite-fast, writes go to the local replica and sync to
    Turso every ``sync_interval`` seconds. Container restart restores
    state from Turso.

The libsql Python package implements DB-API 2.0 and is documented as a
drop-in replacement for sqlite3 in most cases, so callers don't need a
wrapper class — they get back something that quacks like a sqlite3
connection.

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
BACKEND_LIBSQL_REPLICA = "libsql_embedded_replica"


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
        backend=BACKEND_LIBSQL_REPLICA,
        path=path_str,
        sync_url=_redact_host(sync_url),
        sync_interval=sync_interval_seconds,
    )
    conn = libsql.connect(
        path_str,
        sync_url=sync_url,
        auth_token=auth_token,
        sync_interval=sync_interval_seconds,
    )
    # Pull the latest remote snapshot down BEFORE the caller starts
    # executing schema/queries — otherwise a fresh container would race
    # the first sync_interval and see an empty DB.
    try:
        conn.sync()
    except Exception as e:    # noqa: BLE001 — log and re-raise; we want the trace
        _log.error("db_initial_sync_failed", err=str(e))
        raise
    return conn


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
