"""SQLite-backed runtime settings store.

Holds admin-editable values (script prompt, model overrides, defaults) that
change WITHOUT a redeploy. Sits next to the job queue in ``BULKVID_DATA_DIR``.

Two tables:
  - ``settings``  — key/value/updated_at/updated_by
  - ``settings_audit`` — append-only log of every change

Reads are cached for ``cache_ttl_seconds`` to avoid hitting SQLite on every
script generation. Cache is process-local; the worker and the web app each
have their own copy and will diverge for up to a TTL after an edit — fine
for this workload.

Plan §9 (Settings / Admin Panel).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from bulkvid.logging import get_logger
from bulkvid.orchestrator import db as _db

_log = get_logger("settings_store")

# Return type preserved across the reconnect-retry wrapper.
_T = TypeVar("_T")

# How many tries inside ``_run_sync_with_reconnect_retry`` before propagating.
# 2 = one fresh try after one reconnect, which heals a single Hrana stream
# eviction. Persistent Turso outages still propagate so the row processor
# surfaces a real failure instead of silently looping. Plan
# ``_plans/2026-06-24-libsql-hrana-stream-resilience.md``.
_SETTINGS_DB_MAX_ATTEMPTS = 2
# Backoff after a discard-and-reconnect attempt. Short on purpose — the
# common cause is a single stale stream id, and a fresh connection serves
# it on the next try. A real outage propagates anyway after one cycle.
_SETTINGS_DB_RECONNECT_BACKOFF_SECONDS = 0.5


_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_settings_audit_key ON settings_audit(key);
CREATE INDEX IF NOT EXISTS idx_settings_audit_when ON settings_audit(updated_at);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SettingsStore:
    """Async wrapper around a SQLite settings table with TTL cache."""

    def __init__(
        self,
        db_path: Path | str,
        defaults: dict[str, str] | None = None,
        *,
        cache_ttl_seconds: float = 30.0,
        sync_url: str = "",
        auth_token: str = "",
        sync_interval_seconds: float = 1.0,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # Connection params stashed so ``_reconnect_sync`` can rebuild a
        # fresh connection after a Turso flap or Hrana stream eviction wedges
        # the current one. ``_db.connect`` returns plain sqlite3 when
        # ``sync_url`` is empty (dev/tests) and a libsql remote connection
        # otherwise (prod). The reconnect path mirrors
        # ``JobQueue._reconnect_sync`` (queue.py:340). See
        # ``_plans/2026-06-24-libsql-hrana-stream-resilience.md``.
        self._sync_url = sync_url
        self._auth_token = auth_token
        self._sync_interval_seconds = sync_interval_seconds
        self._conn = self._open_connection()
        self._defaults: dict[str, str] = dict(defaults or {})
        self._cache: dict[str, str] = {}
        self._cache_loaded_at = 0.0
        self._cache_ttl = cache_ttl_seconds
        self._lock = asyncio.Lock()
        _log.info(
            "settings_store_init",
            db_path=str(self._db_path),
            default_count=len(self._defaults),
        )

    def _open_connection(self) -> object:
        """Open and fully configure a settings DB connection.

        Used at construction AND by ``_reconnect_sync`` — keeping the setup
        in one place means a reconnected handle is configured identically
        to the original (same row factory, same schema/index guarantees).
        Mirrors ``JobQueue._open_connection`` (queue.py:296).
        """
        conn = _db.connect(
            self._db_path,
            sync_url=self._sync_url,
            auth_token=self._auth_token,
            sync_interval_seconds=self._sync_interval_seconds,
        )
        try:
            conn.row_factory = sqlite3.Row
        except AttributeError:
            _log.warning("row_factory_unsupported", note="dict-like row access disabled")
        conn.executescript("PRAGMA journal_mode=WAL;")
        conn.executescript(_SCHEMA)
        return conn

    def _reconnect_sync(self, *, reason: str) -> None:
        """Throw away the current connection and open a fresh one.

        Cheap-and-dumb on purpose: we never try to *heal* a half-dead libsql
        socket, we replace it. ``self._conn`` is swapped to the new handle
        first so the old one can be closed best-effort. If opening fails
        (Turso fully down) the exception propagates and the caller counts
        it as a failed attempt. Mirrors ``JobQueue._reconnect_sync``
        (queue.py:340). Plan
        ``_plans/2026-06-24-libsql-hrana-stream-resilience.md``.
        """
        old = self._conn
        self._conn = self._open_connection()
        with suppress(Exception):
            old.close()
        _log.warning("settings_store_db_reconnect", reason=reason)

    def _run_sync_with_reconnect_retry(
        self,
        fn: Callable[[], _T],
        *,
        op: str,
        attempts: int = _SETTINGS_DB_MAX_ATTEMPTS,
        backoff_seconds: float = _SETTINGS_DB_RECONNECT_BACKOFF_SECONDS,
    ) -> _T:
        """Run a sync DB op; on failure discard-and-reconnect then retry.

        Mirrors the discard-and-reconnect discipline from ``JobQueue._run_db``
        (queue.py:1081) but synchronous — SettingsStore's callers already run
        each DB op inside ``asyncio.to_thread`` and don't need a per-call
        wall-clock timeout (the SELECTs here read a tiny table). On ANY
        failure (libsql raises an undocumented grab-bag) we throw the
        connection away, open a fresh one, back off, and retry. After
        ``attempts`` failures the last exception propagates so the row
        processor surfaces a real failure rather than silently looping.
        Plan ``_plans/2026-06-24-libsql-hrana-stream-resilience.md``.
        """
        last_exc: BaseException | None = None
        for attempt in range(attempts):
            try:
                return fn()
            except Exception as e:    # libsql throws an undocumented grab-bag
                last_exc = e
                attempts_left = attempts - attempt - 1
                _log.warning(
                    "settings_store_db_call_retry",
                    op=op,
                    attempt=attempt + 1,
                    of=attempts,
                    reason=type(e).__name__,
                    error=str(e)[:200],
                    attempts_left=attempts_left,
                )
                if attempts_left <= 0:
                    break
                try:
                    self._reconnect_sync(reason=f"{op}:{type(e).__name__}")
                except Exception as re:    # reconnect may itself flap
                    _log.warning(
                        "settings_store_reconnect_failed",
                        op=op,
                        error=str(re)[:200],
                    )
                time.sleep(backoff_seconds)
        assert last_exc is not None    # loop entered the except branch
        raise last_exc

    # ── Sync helpers ────────────────────────────────────────────────────────

    def _load_sync(self) -> dict[str, str]:
        # Integer indexing, not ``row["key"]`` / ``row["value"]``: in libsql
        # remote mode (production Turso) the cursor wrapper's description
        # does not expose the column literally named ``key`` under that
        # name, so ``row["key"]`` raises ``IndexError: no column named
        # 'key'``. Chat 2026-06-09 / plan
        # ``_plans/2026-06-09-libsql-key-column-bug.md``: this is the only
        # query in the codebase that reads a column called ``key``, so a
        # targeted positional read sidesteps the libsql quirk without
        # touching the shared ``_DictRow`` shim.
        cur = self._conn.execute("SELECT key, value FROM settings")
        return {row[0]: row[1] for row in cur.fetchall()}

    def _get_sync(self, key: str) -> str | None:
        """Return the stored value for ``key`` or ``None`` if no row exists.

        Distinct from ``get(...)`` because the migration helper has to tell
        ``"value is empty string"`` apart from ``"key was never written"``.

        Positional indexing for the same reason as ``_load_sync``: the
        libsql cursor's description doesn't always carry the original
        column name back from the wire.
        """
        cur = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        return row[0] if row is not None else None

    def _set_sync(self, key: str, value: str, updated_by: str) -> str | None:
        """Set value. Returns old value (or None if new key).

        Uses ``self._conn`` as a transactional context manager when the
        backend supports it (plain sqlite3 in dev/tests). The libsql
        remote connection (production Turso) doesn't implement the
        context-manager protocol and is in autocommit mode anyway, so
        we fall through to direct execution there. Chat 2026-06-09:
        the avatar catalog write blew up with ``TypeError: _LibsqlConn
        object does not support the context manager protocol`` before
        this guard was in place.
        """
        now = _now_iso()
        # Pick a transactional context if the connection supports one;
        # otherwise yield nothing (each execute autocommits on libsql).
        #
        # CRITICAL: check on the TYPE, not the instance. Python's ``with``
        # statement does special method lookup — ``type(obj).__enter__``
        # — which bypasses instance-level ``__getattr__`` proxies. The
        # ``_LibsqlConn`` wrapper has ``__getattr__`` that forwards
        # everything to the inner libsql connection (which DOES have
        # ``__enter__``), so ``hasattr(self._conn, "__enter__")`` returns
        # True even though ``with self._conn:`` then raises
        # ``TypeError: ... does not support the context manager protocol``.
        # Checking on the type avoids the trap.
        from contextlib import nullcontext
        cls = type(self._conn)
        supports_ctx = hasattr(cls, "__enter__") and hasattr(cls, "__exit__")
        txn = self._conn if supports_ctx else nullcontext()
        with txn:
            cur = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            )
            row = cur.fetchone()
            old_value = row["value"] if row is not None else None

            if old_value == value:
                return old_value      # no-op; skip audit row

            self._conn.execute(
                "INSERT INTO settings (key, value, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "  value = excluded.value, "
                "  updated_at = excluded.updated_at, "
                "  updated_by = excluded.updated_by",
                (key, value, now, updated_by),
            )
            self._conn.execute(
                "INSERT INTO settings_audit (key, old_value, new_value, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, old_value, value, now, updated_by),
            )
        return old_value

    def _list_audit_sync(self, key: str | None, limit: int) -> list[sqlite3.Row]:
        if key:
            cur = self._conn.execute(
                "SELECT * FROM settings_audit WHERE key = ? "
                "ORDER BY id DESC LIMIT ?",
                (key, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM settings_audit ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        return cur.fetchall()

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # ── Async API ───────────────────────────────────────────────────────────

    async def _ensure_cache(self) -> dict[str, str]:
        now = time.monotonic()
        if self._cache and (now - self._cache_loaded_at) < self._cache_ttl:
            return self._cache
        async with self._lock:
            if not self._cache or (time.monotonic() - self._cache_loaded_at) >= self._cache_ttl:
                # Turso/Hrana cold-start: the very first SELECT after a
                # fresh container boot occasionally fails with
                # ``Hrana: api error: status=400 ... invalid token``
                # (job-1780936528-524e40fb killed an entire row this way).
                # The retry path almost always succeeds — a short backoff
                # is enough. Re-raise only if both attempts fail so the
                # row processor still surfaces a real failure.
                self._cache = await asyncio.to_thread(
                    self._load_sync_with_retry
                )
                self._cache_loaded_at = time.monotonic()
        return self._cache

    def _load_sync_with_retry(self) -> dict[str, str]:
        """``_load_sync`` with discard-and-reconnect retry. See ``_ensure_cache``.

        Was originally a single retry against the SAME connection to absorb
        a cold-start ``invalid token`` blip. That didn't help when the
        underlying connection's Hrana stream was evicted server-side (Turso
        recycles long-lived/idle streams) — the libsql client clung to the
        dead stream id and every subsequent call 404'd. Now routes through
        ``_run_sync_with_reconnect_retry`` so a stale stream id heals on the
        retry. Plan ``_plans/2026-06-24-libsql-hrana-stream-resilience.md``.
        """
        return self._run_sync_with_reconnect_retry(self._load_sync, op="load")

    async def get(self, key: str, default: str | None = None) -> str:
        """Return the stored value if present, else default, else the registered default."""
        cache = await self._ensure_cache()
        if key in cache:
            return cache[key]
        if default is not None:
            return default
        return self._defaults.get(key, "")

    async def get_all(self) -> dict[str, str]:
        """Return effective values for every registered key (overrides merged onto defaults)."""
        cache = await self._ensure_cache()
        merged = dict(self._defaults)
        merged.update(cache)
        return merged

    async def set(self, key: str, value: str, updated_by: str) -> str | None:
        # Reconnect-retry wrap mirrors ``_load_sync_with_retry`` — a Hrana
        # stream eviction would otherwise brick admin saves (admin panel)
        # the same way it bricked reads. Plan
        # ``_plans/2026-06-24-libsql-hrana-stream-resilience.md``.
        old = await asyncio.to_thread(
            self._run_sync_with_reconnect_retry,
            lambda: self._set_sync(key, value, updated_by),
            op="set",
        )
        # Invalidate cache so the next get reads fresh.
        async with self._lock:
            self._cache_loaded_at = 0.0
            self._cache = {}
        _log.info(
            "settings_changed",
            key=key,
            updated_by=updated_by,
            value_chars=len(value),
        )
        return old

    def migrate_legacy_keys_sync(
        self,
        mapping: dict[str, tuple[str, ...]],
        *,
        updated_by: str = "migration",
    ) -> dict[str, list[str]]:
        """Copy a legacy key's stored value into one or more new keys.

        ``mapping`` is ``{legacy_key: (new_key_1, new_key_2, ...)}``.

        For each ``legacy_key`` that has a row in the store, we copy its
        ``value`` into every ``new_key`` that doesn't already have a row
        (so a previously customized new value is never overwritten). The
        legacy row itself is left in place — it's harmless once it's
        absent from ``SETTINGS_REGISTRY`` and lets us roll back cleanly.

        Returns ``{legacy_key: [new_keys_actually_written]}`` so the caller
        can log what happened.

        Idempotent: a second call after the new keys are populated is a no-op.

        Sync because startup (``_build_state``) and the WSGI entrypoint are
        both sync — keeping this off the event loop avoids forcing those
        callers to manage an asyncio runtime just to run a one-time copy.
        """
        result: dict[str, list[str]] = {}
        for legacy_key, new_keys in mapping.items():
            existing = self._get_sync(legacy_key)
            if existing is None:
                # Legacy row never existed on this deploy — nothing to migrate.
                continue
            written: list[str] = []
            for new_key in new_keys:
                already = self._get_sync(new_key)
                if already is not None:
                    # Admin has already customized the new key; don't overwrite.
                    continue
                self._set_sync(new_key, existing, updated_by)
                written.append(new_key)
            if written:
                _log.info(
                    "settings_migrated",
                    legacy_key=legacy_key,
                    new_keys=written,
                    value_chars=len(existing),
                )
                result[legacy_key] = written
        # Invalidate cache so the next get reads the migrated values.
        self._cache_loaded_at = 0.0
        self._cache = {}
        return result

    async def audit(
        self, key: str | None = None, limit: int = 50
    ) -> list[dict[str, str | None]]:
        # Reconnect-retry wrap mirrors ``_load_sync_with_retry`` — the audit
        # listing is the admin panel's main read; if its connection's Hrana
        # stream is dead, the panel can't show history until restart. Plan
        # ``_plans/2026-06-24-libsql-hrana-stream-resilience.md``.
        rows = await asyncio.to_thread(
            self._run_sync_with_reconnect_retry,
            lambda: self._list_audit_sync(key, limit),
            op="audit",
        )
        return [
            {
                "id": str(r["id"]),
                "key": r["key"],
                "old_value": r["old_value"],
                "new_value": r["new_value"],
                "updated_at": r["updated_at"],
                "updated_by": r["updated_by"],
            }
            for r in rows
        ]

    def defaults(self) -> dict[str, str]:
        return dict(self._defaults)
