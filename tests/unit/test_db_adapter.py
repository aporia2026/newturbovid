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
    assert _db.BACKEND_LIBSQL_REPLICA == "libsql_embedded_replica"


# ── URL redaction ──────────────────────────────────────────────────────────


def test_redact_host_strips_scheme_and_path() -> None:
    assert _db._redact_host("libsql://foo.turso.io/db?token=secret") == "foo.turso.io"


def test_redact_host_strips_userinfo() -> None:
    assert _db._redact_host("https://user:pass@example.com/path") == "example.com"


def test_redact_host_on_bare_host_is_identity() -> None:
    assert _db._redact_host("example.com") == "example.com"
