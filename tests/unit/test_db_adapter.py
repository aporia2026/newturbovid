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
from bulkvid.orchestrator import hrana as _hrana


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
def test_libsqlconn_execute_commit_is_pure_noop_in_autocommit(sql: str) -> None:
    """In autocommit mode (set by db.connect via isolation_level=None),
    libsql has no transaction to commit on ``conn.commit()`` and forwarding
    the call caused stale-read symptoms in remote mode. The wrapper drops
    COMMIT entirely — neither forwards as execute nor calls conn.commit()."""
    fake = _FakeConn()
    wrapped = _db._LibsqlConn(fake)
    wrapped.execute(sql)
    assert fake.committed == 0
    assert fake.rolled_back == 0
    assert fake.executed == []


@pytest.mark.parametrize("sql", ["ROLLBACK", "rollback", "ROLLBACK;", "ROLLBACK TRANSACTION"])
def test_libsqlconn_execute_rollback_is_pure_noop_in_autocommit(sql: str) -> None:
    """Same as COMMIT — see above. In autocommit there's nothing to roll
    back; the wrapper drops the statement."""
    fake = _FakeConn()
    wrapped = _db._LibsqlConn(fake)
    wrapped.execute(sql)
    assert fake.rolled_back == 0
    assert fake.committed == 0
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


def test_libsqlconn_executemany_unrolls_to_execute_calls() -> None:
    """libsql remote mode silently no-ops executemany on at least some
    INSERT shapes (observed prod failure: jobs.INSERT landed, row_queue
    INSERTs via executemany did not, worker JOIN saw empty queue).
    The wrapper unrolls executemany into N execute() calls so each row
    is its own HTTPS round-trip — slower, correct."""
    fake = _FakeConn()
    wrapped = _db._LibsqlConn(fake)
    wrapped.executemany(
        "INSERT INTO row_queue (job_id, row_num, payload, status) VALUES (?,?,?,?)",
        [
            ("j1", 2, "{}", "pending"),
            ("j1", 3, "{}", "pending"),
            ("j1", 4, "{}", "pending"),
        ],
    )
    # Each row was sent as its own execute(), NOT one executemany.
    assert len(fake.executed) == 3
    for sql, params in fake.executed:
        assert sql.startswith("INSERT INTO row_queue")
        assert isinstance(params, tuple)
        assert len(params) == 4
    # Row 2 came first.
    assert fake.executed[0][1][1] == 2
    assert fake.executed[2][1][1] == 4


def test_libsqlconn_executemany_with_empty_seq_is_safe() -> None:
    """Edge case: empty params list shouldn't blow up or commit anything."""
    fake = _FakeConn()
    wrapped = _db._LibsqlConn(fake)
    wrapped.executemany("INSERT INTO row_queue VALUES (?)", [])
    assert fake.executed == []


def test_libsqlconn_tx_pattern_matches_queue_tx_context_manager() -> None:
    """End-to-end check: replicate exactly what queue.py's _tx() does
    (BEGIN IMMEDIATE → work → COMMIT) and confirm the two real INSERT
    statements land while the transaction markers drop on the floor.

    In autocommit-libsql mode (since we pass isolation_level=None),
    each INSERT auto-commits as its own transaction — multi-statement
    atomicity is intentionally traded away to dodge the stale-read
    symptom we hit in remote mode. queue.py tolerates this via
    idempotency keys + recover_orphaned_rows."""
    fake = _FakeConn()
    wrapped = _db._LibsqlConn(fake)

    wrapped.execute("BEGIN IMMEDIATE")
    wrapped.execute("INSERT INTO jobs (job_id, status) VALUES (?, ?)", ("j1", "queued"))
    wrapped.execute("INSERT INTO row_queue (job_id, row_num) VALUES (?, ?)", ("j1", 2))
    wrapped.execute("COMMIT")

    # The two DML statements landed.
    assert len(fake.executed) == 2
    # BEGIN/COMMIT did NOT cause any extra commit() or rollback() calls
    # — they're pure no-ops at this layer.
    assert fake.committed == 0
    assert fake.rolled_back == 0


def test_libsqlconn_tx_pattern_with_rollback_drops_rollback_on_the_floor() -> None:
    """Error-path counterpart. In autocommit mode the prior INSERT already
    committed, so an ``execute("ROLLBACK")`` cannot undo it. The wrapper
    drops the ROLLBACK statement entirely rather than trying to call
    conn.rollback() (which on an autocommit libsql conn produced odd
    behaviour in prod)."""
    fake = _FakeConn()
    wrapped = _db._LibsqlConn(fake)
    wrapped.execute("BEGIN IMMEDIATE")
    wrapped.execute("INSERT INTO jobs (job_id) VALUES (?)", ("j1",))
    wrapped.execute("ROLLBACK")
    assert len(fake.executed) == 1
    assert fake.rolled_back == 0
    assert fake.committed == 0


# ── Hrana HTTP transport selection (Plan 2026-08-12) ────────────────────────


class _FakeHranaClient:
    """Stands in for a real ``HranaClient``. Records statements so the shim's
    BEGIN/COMMIT translation can be asserted by what it did NOT send."""

    def __init__(self, *, fail_probe: bool = False) -> None:
        self.statements: list[tuple[str, object]] = []
        self.scripts: list[str] = []
        self.closed = False
        self._fail_probe = fail_probe
        self.result = _hrana.StatementResult(
            cols=(), rows=[], affected_row_count=0, last_insert_rowid=None
        )

    def execute(self, sql: str, params: object = ()) -> _hrana.StatementResult:
        if self._fail_probe:
            raise _hrana.HranaTransportError("probe boom")
        self.statements.append((sql, params))
        return self.result

    def execute_script(self, sql: str) -> None:
        self.scripts.append(sql)

    def close(self) -> None:
        self.closed = True


def _rowset(cols, rows, affected=0, rowid=None) -> _hrana.StatementResult:
    return _hrana.StatementResult(
        cols=tuple(cols), rows=[tuple(r) for r in rows],
        affected_row_count=affected, last_insert_rowid=rowid,
    )


def test_db_transport_defaults_to_libsql(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BULKVID_DB_TRANSPORT", raising=False)
    assert _db.db_transport() == _db.TRANSPORT_LIBSQL


def test_db_transport_reads_hrana_and_ignores_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BULKVID_DB_TRANSPORT", " HRANA ")
    assert _db.db_transport() == _db.TRANSPORT_HRANA
    # An unrecognised value must never fail a deploy; it falls back.
    monkeypatch.setenv("BULKVID_DB_TRANSPORT", "postgres")
    assert _db.db_transport() == _db.TRANSPORT_LIBSQL


def test_hrana_conn_returns_dict_rows(tmp_path: Path) -> None:
    """Rows must be indistinguishable from the libsql path: name AND index
    access, because queue.py uses both."""
    client = _FakeHranaClient()
    client.result = _rowset(("job_id", "status"), [("job-1", "queued")])
    conn = _db._HranaConn(client)

    row = conn.execute("SELECT job_id, status FROM jobs").fetchone()
    assert row is not None
    assert row["job_id"] == "job-1"      # by name
    assert row[1] == "queued"            # by index
    assert row.keys() == ["job_id", "status"]


def test_hrana_conn_translates_transaction_statements_to_noops() -> None:
    """Autocommit means BEGIN/COMMIT/ROLLBACK must never reach the wire. On a
    stateless transport a forwarded BEGIN would be actively misleading: it
    cannot hold anything open across requests."""
    client = _FakeHranaClient()
    conn = _db._HranaConn(client)

    for sql in ("BEGIN IMMEDIATE", "COMMIT", "ROLLBACK", "begin", "End"):
        cur = conn.execute(sql)
        assert cur.fetchone() is None
    assert client.statements == []       # nothing was sent

    conn.execute("SELECT 1")
    assert [s for s, _ in client.statements] == ["SELECT 1"]


def test_hrana_conn_exposes_rowcount_and_lastrowid() -> None:
    client = _FakeHranaClient()
    client.result = _rowset((), [], affected=4, rowid=99)
    conn = _db._HranaConn(client)
    cur = conn.execute("UPDATE row_queue SET status = ?", ("pending",))
    assert cur.rowcount == 4
    assert cur.lastrowid == 99


def test_hrana_cursor_fetch_semantics() -> None:
    client = _FakeHranaClient()
    client.result = _rowset(("n",), [(1,), (2,), (3,)])
    conn = _db._HranaConn(client)

    cur = conn.execute("SELECT n FROM t")
    assert cur.fetchone()["n"] == 1
    assert [r["n"] for r in cur.fetchmany(1)] == [2]
    assert [r["n"] for r in cur.fetchall()] == [3]
    assert cur.fetchone() is None        # exhausted
    # Iteration walks a fresh cursor from the start.
    assert [r["n"] for r in conn.execute("SELECT n FROM t")] == [1, 2, 3]


def test_hrana_conn_executescript_uses_sequence() -> None:
    client = _FakeHranaClient()
    conn = _db._HranaConn(client)
    conn.executescript("CREATE TABLE a(x); CREATE TABLE b(y);")
    assert client.scripts == ["CREATE TABLE a(x); CREATE TABLE b(y);"]


def test_hrana_conn_executemany_unrolls_to_one_call_per_row() -> None:
    client = _FakeHranaClient()
    conn = _db._HranaConn(client)
    conn.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
    assert [p for _, p in client.statements] == [(1,), (2,)]


def test_hrana_conn_accepts_row_factory_assignment() -> None:
    """queue.py assigns ``conn.row_factory = sqlite3.Row`` unconditionally; the
    shim must tolerate it rather than raise."""
    conn = _db._HranaConn(_FakeHranaClient())
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT 1") is not None


def test_hrana_conn_close_closes_the_client() -> None:
    client = _FakeHranaClient()
    _db._HranaConn(client).close()
    assert client.closed is True


def test_connect_uses_hrana_when_selected_and_probe_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _FakeHranaClient()
    monkeypatch.setenv("BULKVID_DB_TRANSPORT", "hrana")
    monkeypatch.setattr(_hrana, "HranaClient", lambda *a, **k: client)
    conn = _db.connect(
        tmp_path / "x.db", sync_url="libsql://x.turso.io", auth_token="tok"
    )
    assert isinstance(conn, _db._HranaConn)
    # The probe is a real round-trip, so a broken transport is caught at open.
    assert client.statements == [("SELECT 1", ())]


def test_connect_falls_back_to_libsql_when_probe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safety property that makes the env flag safe to flip on a live
    Space: a broken HTTP transport degrades to the client that already works
    instead of crash-looping both processes under supervisord."""
    import sys
    import types

    monkeypatch.setenv("BULKVID_DB_TRANSPORT", "hrana")
    monkeypatch.setattr(
        _hrana, "HranaClient", lambda *a, **k: _FakeHranaClient(fail_probe=True)
    )
    # Stand in for the optional libsql package (absent on most dev machines).
    fake_libsql = types.ModuleType("libsql")
    fake_libsql.connect = lambda *a, **k: sqlite3.connect(":memory:")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "libsql", fake_libsql)

    conn = _db.connect(
        tmp_path / "x.db", sync_url="libsql://x.turso.io", auth_token="tok"
    )
    assert isinstance(conn, _db._LibsqlConn)
    assert not isinstance(conn, _db._HranaConn)


# ── PRAGMA handling + deep probe (2026-08-12 rollback regression) ───────────


def test_pragma_script_is_dropped_not_sent() -> None:
    """REGRESSION (2026-08-12): ``PRAGMA journal_mode=WAL;`` runs at every
    connection open and Turso answers HTTP 400 "SQL not allowed statement",
    which crash-looped both processes. A remote server owns its own journal
    mode, so the statement has nothing to act on and must never reach the wire.
    """
    client = _FakeHranaClient()
    conn = _db._HranaConn(client)
    conn.executescript("PRAGMA journal_mode=WAL;")
    assert client.scripts == []
    assert client.statements == []


def test_standalone_pragma_execute_is_a_noop() -> None:
    client = _FakeHranaClient()
    conn = _db._HranaConn(client)
    assert conn.execute("PRAGMA foreign_keys=ON").fetchone() is None
    assert client.statements == []


def test_schema_script_containing_ddl_is_still_forwarded_intact() -> None:
    """The pragma filter must not swallow real schema work. A script is only
    dropped when EVERY statement in it is a pragma."""
    client = _FakeHranaClient()
    conn = _db._HranaConn(client)
    schema = "CREATE TABLE jobs (job_id TEXT);\nCREATE INDEX i ON jobs(job_id);"
    conn.executescript(schema)
    assert client.scripts == [schema]


def test_mixed_pragma_and_ddl_script_is_forwarded_intact() -> None:
    """Belt and braces: a script that merely STARTS with a pragma still carries
    DDL, so it must go through untouched rather than be dropped."""
    client = _FakeHranaClient()
    conn = _db._HranaConn(client)
    mixed = "PRAGMA journal_mode=WAL; CREATE TABLE t (x INTEGER);"
    conn.executescript(mixed)
    assert client.scripts == [mixed]


def test_connect_probe_exercises_ddl_and_query_not_just_select_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original probe ran only ``SELECT 1``, passed, and let boot die on the
    next statement. The probe must run the same SHAPES the caller runs."""
    client = _FakeHranaClient()
    monkeypatch.setenv("BULKVID_DB_TRANSPORT", "hrana")
    monkeypatch.setattr(_hrana, "HranaClient", lambda *a, **k: client)

    conn = _db.connect(
        tmp_path / "x.db", sync_url="libsql://x.turso.io", auth_token="tok"
    )
    assert isinstance(conn, _db._HranaConn)
    # DDL-over-sequence was proven before committing to the transport...
    assert any("CREATE TABLE IF NOT EXISTS" in s for s in client.scripts)
    assert any("DROP TABLE" in s for s in client.scripts)
    # ...and the query path too. The pragma script never reaches the wire.
    assert [s for s, _ in client.statements] == ["SELECT 1"]
    assert not any("PRAGMA" in s for s in client.scripts)


def test_connect_falls_back_when_ddl_probe_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case that took production down: a statement the server refuses. With
    the deeper probe this now degrades to a fallback instead of a crash-loop."""
    import sys
    import types

    class _RejectsDdl(_FakeHranaClient):
        def execute_script(self, sql: str) -> None:
            raise _hrana.HranaTransportError(
                'HTTP 400: {"error":"SQL not allowed statement"}'
            )

    monkeypatch.setenv("BULKVID_DB_TRANSPORT", "hrana")
    monkeypatch.setattr(_hrana, "HranaClient", lambda *a, **k: _RejectsDdl())
    fake_libsql = types.ModuleType("libsql")
    fake_libsql.connect = lambda *a, **k: sqlite3.connect(":memory:")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "libsql", fake_libsql)

    conn = _db.connect(
        tmp_path / "x.db", sync_url="libsql://x.turso.io", auth_token="tok"
    )
    assert isinstance(conn, _db._LibsqlConn)


def test_turso_like_server_rejecting_pragma_no_longer_breaks_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-08-12 production failure, reproduced end to end.

    Real Turso answers HTTP 400 to ``PRAGMA journal_mode=WAL``. Against a client
    that behaves that way, connect() must still select the HTTP transport (not
    fall back), and the pragma call that ``JobQueue._open_connection`` makes
    immediately afterwards must succeed rather than crash-loop the process."""
    class _TursoLike(_FakeHranaClient):
        def execute_script(self, sql: str) -> None:
            if "PRAGMA" in sql.upper():
                raise _hrana.HranaTransportError(
                    'HTTP 400: {"error":"SQL not allowed statement: '
                    'PRAGMA journal_mode=WAL;"}'
                )
            super().execute_script(sql)

    monkeypatch.setenv("BULKVID_DB_TRANSPORT", "hrana")
    monkeypatch.setattr(_hrana, "HranaClient", lambda *a, **k: _TursoLike())

    conn = _db.connect(
        tmp_path / "x.db", sync_url="libsql://x.turso.io", auth_token="tok"
    )
    assert isinstance(conn, _db._HranaConn)      # transport selected, no fallback
    conn.executescript("PRAGMA journal_mode=WAL;")   # the call that killed prod
    conn.executescript("CREATE TABLE IF NOT EXISTS jobs (job_id TEXT);")
    assert conn.execute("SELECT 1") is not None
