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
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            timeout=30.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
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
                self._cache = await asyncio.to_thread(self._load_sync)
                self._cache_loaded_at = time.monotonic()
        return self._cache

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
