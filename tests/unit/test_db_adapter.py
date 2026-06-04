"""Tests for the DB backend selector.

The sqlite3 path is the dev/test path; the libsql/Turso path is exercised
at deploy time (see ``_plans/2026-06-04-migrate-to-hf-spaces-turso.md``).
These tests pin down the selector behaviour we DO control here:

  - Empty ``sync_url`` returns a real ``sqlite3.Connection``.
  - A set ``sync_url`` with empty ``auth_token`` raises a clear
    ``ValueError`` (so a misconfigured deploy fails fast on boot, not
    silently three minutes later).
  - ``ping`` works against any DB-API connection and returns elapsed ms.
  - URL redaction trims auth/query bits so logs stay safe.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bulkvid.orchestrator import db as _db


def test_connect_with_empty_sync_url_returns_sqlite_connection(tmp_path: Path) -> None:
    conn = _db.connect(tmp_path / "x.db")
    assert isinstance(conn, sqlite3.Connection)
    # Sanity: it behaves like sqlite3 — same isolation level our code expects.
    assert conn.isolation_level is None
    conn.close()


def test_connect_creates_parent_dir(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "subdir" / "x.db"
    conn = _db.connect(target)
    assert target.parent.is_dir()
    conn.close()


def test_connect_with_sync_url_and_no_token_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="BULKVID_DB_AUTH_TOKEN"):
        _db.connect(tmp_path / "x.db", sync_url="libsql://example.turso.io")


def test_ping_returns_positive_ms(tmp_path: Path) -> None:
    conn = _db.connect(tmp_path / "x.db")
    elapsed = _db.ping(conn)
    assert elapsed >= 0.0
    assert elapsed < 1000.0    # sub-second on a local sqlite, obviously
    conn.close()


def test_backend_constants_are_strings() -> None:
    """Boot logs reference these by import; pin the spellings so a typo
    breaks tests rather than silently degrading observability."""
    assert _db.BACKEND_SQLITE == "sqlite_local"
    assert _db.BACKEND_LIBSQL_REMOTE == "libsql_remote"
    # Legacy constant kept for backwards compatibility with prior plan
    # references; pinned so a rename trips this test.
    assert _db.BACKEND_LIBSQL_REPLICA == "libsql_embedded_replica"


# ── URL redaction ──────────────────────────────────────────────────────────


def test_redact_host_strips_scheme_and_path() -> None:
    assert _db._redact_host("libsql://foo.turso.io/db?token=secret") == "foo.turso.io"


def test_redact_host_strips_userinfo() -> None:
    assert _db._redact_host("https://user:pass@example.com/path") == "example.com"


def test_redact_host_on_bare_host_is_identity() -> None:
    assert _db._redact_host("example.com") == "example.com"


# ── libsql tuple-to-dict shims ─────────────────────────────────────────────
# These test the layer that lets queue.py + settings_store.py keep using
# ``row["col_name"]`` access even though libsql's raw cursors return
# plain tuples. We can't install libsql on every dev box (no Python 3.14
# wheel), so these tests exercise the wrapper against a fake "libsql-like"
# connection that mimics tuple-returning cursors.


class _FakeCursor:
    """Plain-tuple cursor matching libsql's actual surface — what _LibsqlCursor
    has to wrap. Returns tuples from fetchone/fetchall/fetchmany; exposes
    description, rowcount, lastrowid."""

    def __init__(
        self,
        rows: list[tuple] | None = None,
        description: list[tuple] | None = None,
        rowcount: int = 0,
        lastrowid: int | None = None,
    ) -> None:
        self._rows = list(rows or [])
        self.description = description
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self) -> tuple | None:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[tuple]:
        out, self._rows = self._rows, []
        return out

    def fetchmany(self, size: int | None = None) -> list[tuple]:
        n = size or 1
        out = self._rows[:n]
        self._rows = self._rows[n:]
        return out

    def __iter__(self):
        while self._rows:
            yield self._rows.pop(0)


def test_dictrow_supports_string_and_int_indexing() -> None:
    row = _db._DictRow(("job-1", "queued", 3), ("job_id", "status", "row_count"))
    assert row["job_id"] == "job-1"
    assert row["status"] == "queued"
    assert row["row_count"] == 3
    # Index access still works (sqlite3.Row supports both).
    assert row[0] == "job-1"
    assert row[2] == 3


def test_dictrow_keys_returns_column_names() -> None:
    row = _db._DictRow(("a", "b"), ("c1", "c2"))
    assert row.keys() == ["c1", "c2"]


def test_dictrow_iteration_yields_values() -> None:
    row = _db._DictRow(("a", "b"), ("c1", "c2"))
    assert list(row) == ["a", "b"]


def test_dictrow_dict_comprehension_pattern_matches_queue_code() -> None:
    """queue._get_job_sync uses ``Job(**{k: row[k] for k in row.keys()})``.
    Pin that exact pattern so it never silently degrades."""
    row = _db._DictRow(("job-1", "queued"), ("job_id", "status"))
    assembled = {k: row[k] for k in row.keys()}
    assert assembled == {"job_id": "job-1", "status": "queued"}


def test_dictrow_unknown_column_raises_indexerror() -> None:
    row = _db._DictRow(("a",), ("c1",))
    with pytest.raises(IndexError, match="no column named"):
        _ = row["nope"]


def test_libsqlcursor_fetchone_wraps_tuple_in_dictrow() -> None:
    fake = _FakeCursor(
        rows=[("job-1", "queued")],
        description=[("job_id",), ("status",)],
    )
    wrapped = _db._LibsqlCursor(fake)
    row = wrapped.fetchone()
    assert row is not None
    assert row["job_id"] == "job-1"
    assert row["status"] == "queued"


def test_libsqlcursor_fetchone_passes_through_none_at_eof() -> None:
    fake = _FakeCursor(rows=[], description=[("c",)])
    assert _db._LibsqlCursor(fake).fetchone() is None


def test_libsqlcursor_fetchall_returns_list_of_dictrows() -> None:
    fake = _FakeCursor(
        rows=[("1", "a"), ("2", "b")],
        description=[("id",), ("name",)],
    )
    rows = _db._LibsqlCursor(fake).fetchall()
    assert len(rows) == 2
    assert rows[0]["id"] == "1"
    assert rows[1]["name"] == "b"


def test_libsqlcursor_forwards_rowcount_and_lastrowid() -> None:
    fake = _FakeCursor(rowcount=7, lastrowid=42)
    wrapped = _db._LibsqlCursor(fake)
    assert wrapped.rowcount == 7
    assert wrapped.lastrowid == 42


class _FakeConn:
    """Fake libsql-shaped connection for testing _LibsqlConn forwarding."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.committed = 0
        self.rolled_back = 0
        self.closed = False

    def execute(self, sql: str, params: Any = ()) -> _FakeCursor:
        self.executed.append((sql, params))
        return _FakeCursor(
            rows=[("v",)],
            description=[("col",)],
            rowcount=1,
        )

    def executemany(self, sql: str, params_seq: Any) -> _FakeCursor:
        self.executed.append((sql, list(params_seq)))
        return _FakeCursor(rowcount=len(list(params_seq)))

    def executescript(self, sql: str) -> str:
        self.executed.append(("script", sql))
        return "ok"

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed = True


def test_libsqlconn_execute_returns_wrapped_cursor() -> None:
    fake = _FakeConn()
    wrapped = _db._LibsqlConn(fake)
    cur = wrapped.execute("SELECT col FROM t")
    assert isinstance(cur, _db._LibsqlCursor)
    assert cur.fetchone()["col"] == "v"


def test_libsqlconn_accepts_row_factory_assignment_silently() -> None:
    """queue.py and settings_store.py do
    ``self._conn.row_factory = sqlite3.Row``. The wrapper must accept that
    assignment without raising — even though row_factory has no effect
    (every cursor already returns _DictRow)."""
    fake = _FakeConn()
    wrapped = _db._LibsqlConn(fake)
    wrapped.row_factory = sqlite3.Row    # must not raise
    assert wrapped.row_factory is sqlite3.Row


def test_libsqlconn_forwards_commit_rollback_close() -> None:
    fake = _FakeConn()
    wrapped = _db._LibsqlConn(fake)
    wrapped.commit()
    wrapped.rollback()
    wrapped.close()
    assert fake.committed == 1
    assert fake.rolled_back == 1
    assert fake.closed is True


def test_libsqlconn_sync_noop_when_underlying_lacks_method() -> None:
    """Remote-mode libsql connections don't expose .sync(); make sure the
    wrapper doesn't blow up on those."""
    fake = _FakeConn()    # no sync attribute
    wrapped = _db._LibsqlConn(fake)
    assert wrapped.sync() is None    # should not raise


def test_libsqlconn_passes_through_unknown_attrs() -> None:
    fake = _FakeConn()
    fake.in_transaction = True    # type: ignore[attr-defined]
    wrapped = _db._LibsqlConn(fake)
    assert wrapped.in_transaction is True


# ── Transaction-statement translation (the wal_insert_begin failure) ───────
# libsql manages WAL transactions internally; raw execute("COMMIT") raises
# ``ValueError: wal_insert_begin failed`` because it tries to start a fresh
# transaction to commit, with no actual transaction open. queue.py's _tx()
# was written for sqlite3 autocommit-with-explicit-BEGIN/COMMIT semantics,
# so the wrapper has to translate those three statements into libsql's
# native commit()/rollback() calls.


@pytest.mark.parametrize(
    "sql",
    ["BEGIN", "BEGIN IMMEDIATE", "begin immediate", "BEGIN EXCLUSIVE", "BEGIN DEFERRED"],
)
def test_libsqlconn_execute_begin_is_noop(sql: str) -> None:
    fake = _FakeConn()
    wrapped = _db._LibsqlConn(fake)
    cur = wrapped.execute(sql)
    # No-op cursor; nothing forwarded to libsql.
    assert fake.executed == []
    assert cur.fetchone() is None    # no rows from a BEGIN
    assert cur.fetchall() == []


@pytest.mark.parametrize("sql", ["COMMIT", "commit", "COMMIT;", "END", "END TRANSACTION"])
def test_libsqlconn_execute_commit_translates_to_native_commit(sql: str) -> None:
    fake = _FakeConn()
    wrapped = _db._LibsqlConn(fake)
    wrapped.execute(sql)
    assert fake.committed == 1
    # Did NOT forward as a regular execute (which would push to the
    # ``executed`` list and trigger the libsql wal_insert_begin bug).
    assert fake.executed == []


@pytest.mark.parametrize("sql", ["ROLLBACK", "rollback", "ROLLBACK;", "ROLLBACK TRANSACTION"])
def test_libsqlconn_execute_rollback_translates_to_native_rollback(sql: str) -> None:
    fake = _FakeConn()
    wrapped = _db._LibsqlConn(fake)
    wrapped.execute(sql)
    assert fake.rolled_back == 1
    assert fake.executed == []


def test_libsqlconn_passes_regular_sql_through_untouched() -> None:
    """Defense: we only intercept transaction statements. Regular DML/DDL
    must still hit libsql so the data actually lands."""
    fake = _FakeConn()
    wrapped = _db._LibsqlConn(fake)
    wrapped.execute("INSERT INTO jobs (job_id) VALUES (?)", ("job-1",))
    wrapped.execute("SELECT * FROM jobs WHERE job_id = ?", ("job-1",))
    assert len(fake.executed) == 2
    assert fake.committed == 0    # no implicit commit
    assert fake.rolled_back == 0


def test_libsqlconn_tx_pattern_matches_queue_tx_context_manager() -> None:
    """End-to-end check: replicate exactly what queue.py's _tx() does
    (BEGIN IMMEDIATE → work → COMMIT) and confirm we end up with one
    libsql .commit() call and zero broken execute("COMMIT") forwards."""
    fake = _FakeConn()
    wrapped = _db._LibsqlConn(fake)

    wrapped.execute("BEGIN IMMEDIATE")
    wrapped.execute("INSERT INTO jobs (job_id, status) VALUES (?, ?)", ("j1", "queued"))
    wrapped.execute("INSERT INTO row_queue (job_id, row_num) VALUES (?, ?)", ("j1", 2))
    wrapped.execute("COMMIT")

    # The two DML statements landed. The BEGIN/COMMIT were translated.
    assert len(fake.executed) == 2
    assert fake.committed == 1
    assert fake.rolled_back == 0


def test_libsqlconn_tx_pattern_with_rollback_matches_queue_tx_on_error() -> None:
    """Same end-to-end check, error path."""
    fake = _FakeConn()
    wrapped = _db._LibsqlConn(fake)
    wrapped.execute("BEGIN IMMEDIATE")
    wrapped.execute("INSERT INTO jobs (job_id) VALUES (?)", ("j1",))
    wrapped.execute("ROLLBACK")
    assert len(fake.executed) == 1
    assert fake.rolled_back == 1
    assert fake.committed == 0
