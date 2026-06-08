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
from datetime import datetime, timezone
from pathlib import Path

from bulkvid.logging import get_logger
from bulkvid.orchestrator import db as _db

_log = get_logger("settings_store")


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
        # See ``JobQueue.__init__`` for the same backend-selection rationale.
        # ``sync_url`` empty → plain sqlite3 (dev/tests); set → Turso via the
        # libsql embedded replica.
        self._conn = _db.connect(
            self._db_path,
            sync_url=sync_url,
            auth_token=auth_token,
            sync_interval_seconds=sync_interval_seconds,
        )
        try:
            self._conn.row_factory = sqlite3.Row
        except AttributeError:
            _log.warning("row_factory_unsupported", note="dict-like row access disabled")
        self._conn.executescript("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
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

    # ── Sync helpers ────────────────────────────────────────────────────────

    def _load_sync(self) -> dict[str, str]:
        cur = self._conn.execute("SELECT key, value FROM settings")
        return {row["key"]: row["value"] for row in cur.fetchall()}

    def _get_sync(self, key: str) -> str | None:
        """Return the stored value for ``key`` or ``None`` if no row exists.

        Distinct from ``get(...)`` because the migration helper has to tell
        ``"value is empty string"`` apart from ``"key was never written"``.
        """
        cur = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        return row["value"] if row is not None else None

    def _set_sync(self, key: str, value: str, updated_by: str) -> str | None:
        """Set value. Returns old value (or None if new key)."""
        now = _now_iso()
        with self._conn:
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
        """``_load_sync`` with one cold-start retry. See ``_ensure_cache``."""
        try:
            return self._load_sync()
        except Exception as e:    # broad: libSQL raises ValueError, sqlite3 raises OperationalError
            _log.warning(
                "settings_store_load_retry",
                error=str(e)[:200],
                error_type=type(e).__name__,
            )
            time.sleep(0.5)
            return self._load_sync()

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
        old = await asyncio.to_thread(self._set_sync, key, value, updated_by)
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
        rows = await asyncio.to_thread(self._list_audit_sync, key, limit)
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
