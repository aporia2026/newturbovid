"""Tests for the SQLite-backed runtime settings store."""

from __future__ import annotations

from pathlib import Path

import pytest

from bulkvid.orchestrator.settings_store import SettingsStore


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    s = SettingsStore(
        tmp_path / "settings.db",
        defaults={"script_system_prompt": "default-prompt", "kie_model": "nano-banana"},
        cache_ttl_seconds=0.0,  # disable cache to make changes immediate in tests
    )
    yield s
    s.close()


async def test_get_returns_registered_default_when_unset(store: SettingsStore) -> None:
    assert await store.get("script_system_prompt") == "default-prompt"


async def test_set_works_when_connection_lacks_context_manager(
    store: SettingsStore,
) -> None:
    """Regression for chat 2026-06-09: the production libsql remote
    connection wrapper (``_LibsqlConn``) does NOT support the
    context-manager protocol on the type, so ``with self._conn:`` raises
    ``TypeError: '_LibsqlConn' object does not support the context
    manager protocol``. The avatar picker tripped on this when it tried
    to cache the auto-fetched list.

    The proxy below mimics ``_LibsqlConn`` exactly: its CLASS doesn't
    define ``__enter__`` / ``__exit__``, but its ``__getattr__``
    forwards EVERYTHING to an inner sqlite3 connection (which DOES
    have those dunders). This causes ``hasattr(proxy, "__enter__")``
    to return True even though ``with proxy:`` fails — the same trap
    the production code fell into. Checking on the type avoids it.
    """
    real_conn = store._conn

    class _LibsqlLikeProxy:
        """Same shape as the real ``_LibsqlConn``: no ``__enter__`` on
        the class, but ``__getattr__`` forwards to the inner conn."""

        def __init__(self, inner):
            self.__dict__["_inner"] = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __setattr__(self, name, value):
            setattr(self._inner, name, value)

    proxy = _LibsqlLikeProxy(real_conn)

    # Sanity-check the trap: hasattr returns True on the instance (via
    # __getattr__ forwarding to sqlite3.Connection which has __enter__),
    # but `with` would fail because type-based lookup bypasses
    # __getattr__. If this assertion ever changes, the production bug
    # has resurfaced.
    assert hasattr(proxy, "__enter__"), (
        "test proxy isn't mimicking _LibsqlConn correctly — "
        "instance-level hasattr should still return True"
    )
    assert not hasattr(type(proxy), "__enter__"), (
        "test proxy isn't mimicking _LibsqlConn correctly — "
        "the class must NOT define __enter__"
    )

    store._conn = proxy    # type: ignore[assignment]
    try:
        # Smoke test: set must succeed via the autocommit fallback path.
        old = await store.set(
            "kie_model", "first-write", updated_by="regression-test",
        )
        assert old is None    # never written before
        assert await store.get("kie_model") == "first-write"
        # Idempotent re-write: same value returns the old value.
        old = await store.set(
            "kie_model", "first-write", updated_by="regression-test",
        )
        assert old == "first-write"
        # Updating to a new value also works.
        old = await store.set(
            "kie_model", "updated", updated_by="regression-test",
        )
        assert old == "first-write"
        assert await store.get("kie_model") == "updated"
    finally:
        store._conn = real_conn    # type: ignore[assignment]


async def test_ensure_cache_retries_once_on_cold_start_failure(store: SettingsStore) -> None:
    """Regression for job-1780936528-524e40fb row 3.

    The first SELECT after a fresh container boot occasionally fails
    with ``Hrana: api error: status=400 ... invalid token`` (Turso/libSQL
    cold-start). ``_ensure_cache`` now retries ``_load_sync`` once with
    a short sleep so a transient cold-start blip doesn't kill a row.
    """
    real_load = store._load_sync
    call_count = {"n": 0}

    def flaky_load() -> dict[str, str]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError(
                "Hrana: `api error: `status=400 Bad Request, body="
                '{"error":"Protocol error: failed to parse http request: invalid token"}``'
            )
        return real_load()

    store._load_sync = flaky_load    # type: ignore[method-assign]

    # First read triggers _ensure_cache. Without the retry this would
    # raise the Hrana ValueError; with the retry it succeeds on attempt 2.
    value = await store.get("script_system_prompt")
    assert value == "default-prompt"     # registered default, table is empty
    assert call_count["n"] == 2          # one failure + one retry


async def test_ensure_cache_reraises_when_both_attempts_fail(store: SettingsStore) -> None:
    """If both attempts fail, the error must propagate — the row
    processor surfaces it as a row failure rather than silently
    succeeding with an empty cache."""
    def always_fails() -> dict[str, str]:
        raise ValueError("Hrana: persistent failure")

    store._load_sync = always_fails    # type: ignore[method-assign]

    with pytest.raises(ValueError, match="Hrana"):
        await store.get("script_system_prompt")


async def test_load_sync_reconnects_after_stream_eviction(
    store: SettingsStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for job-61d63442da7e6b25 (2026-06-24).

    Turso evicted the Hrana stream id our libsql client was holding; every
    call against that connection 404'd with ``stream not found: <id>``. The
    old ``_load_sync_with_retry`` slept 0.5 s and retried the SAME dead
    connection — same id, same 404. Result: every row failed fast with
    ``unhandled: Hrana: api error: status=404 Not Found, body={"error":
    "stream not found: 14edb5a7:1454bfd"}`` until the container restarted.

    Fix: discard-and-reconnect before retrying. This test simulates the
    pathology — ``_load_sync`` raises a stream-shaped error the first time,
    then succeeds — and asserts ``_reconnect_sync`` fires between the two
    attempts so the second call runs against a fresh handle. Plan
    ``_plans/2026-06-24-libsql-hrana-stream-resilience.md``.
    """
    monkeypatch.setattr(
        "bulkvid.orchestrator.settings_store._SETTINGS_DB_RECONNECT_BACKOFF_SECONDS",
        0.0,
    )

    real_load = store._load_sync
    call_count = {"n": 0}

    def stream_dead_then_recover() -> dict[str, str]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError(
                'Hrana: `api error: `status=404 Not Found, body='
                '{"error":"stream not found: 14edb5a7:1454bfd"}``'
            )
        return real_load()

    store._load_sync = stream_dead_then_recover    # type: ignore[method-assign]

    reconnects = {"n": 0}
    real_reconnect = store._reconnect_sync

    def spy_reconnect(*, reason: str) -> None:
        reconnects["n"] += 1
        real_reconnect(reason=reason)

    store._reconnect_sync = spy_reconnect    # type: ignore[method-assign]

    value = await store.get("script_system_prompt")
    assert value == "default-prompt"     # registered default, table is empty
    assert call_count["n"] == 2          # one failure + one retry
    assert reconnects["n"] == 1          # connection swapped between attempts


async def test_set_reconnects_after_stream_eviction(
    store: SettingsStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Hrana stream death during an admin save (settings panel write) was
    bricking the admin panel the same way reads got bricked — the user had
    to wait for a redeploy to change a setting. Reconnect-on-failure means
    the second attempt lands on a fresh stream. Plan
    ``_plans/2026-06-24-libsql-hrana-stream-resilience.md``."""
    monkeypatch.setattr(
        "bulkvid.orchestrator.settings_store._SETTINGS_DB_RECONNECT_BACKOFF_SECONDS",
        0.0,
    )

    real_set = store._set_sync
    call_count = {"n": 0}

    def stream_dead_then_recover(key: str, value: str, updated_by: str) -> str | None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError(
                'Hrana: `api error: `status=404 Not Found, body='
                '{"error":"stream not found: ab12:cd34"}``'
            )
        return real_set(key, value, updated_by)

    store._set_sync = stream_dead_then_recover    # type: ignore[method-assign]

    reconnects = {"n": 0}
    real_reconnect = store._reconnect_sync

    def spy_reconnect(*, reason: str) -> None:
        reconnects["n"] += 1
        real_reconnect(reason=reason)

    store._reconnect_sync = spy_reconnect    # type: ignore[method-assign]

    old = await store.set("kie_model", "post-reconnect", updated_by="regression")
    assert old is None                  # first write of this key
    assert call_count["n"] == 2         # one failure + one retry
    assert reconnects["n"] == 1         # connection swapped between attempts


async def test_load_sync_reads_by_index_not_by_column_name(
    store: SettingsStore,
) -> None:
    """Regression for chat 2026-06-09 / HF outage:
    ``IndexError: no column named 'key'``.

    In libsql remote mode the cursor's ``description`` does not always
    expose the column literally named ``key`` under that name, so
    ``row["key"]`` raises ``IndexError``. The bug was latent for weeks
    — it only fired once the settings table got its first row (the
    avatar catalog write). This test wraps the real sqlite3 cursor in
    a proxy that hides the column-name path entirely; if ``_load_sync``
    or ``_get_sync`` ever regresses to ``row["key"]`` / ``row["value"]``
    instead of positional indexing, this test fails fast.
    """
    # Seed a real row so the dict comprehension actually iterates.
    await store.set("kie_model", "anchor-value", updated_by="regression-test")

    real_conn = store._conn

    class _NamelessCursor:
        """Wraps a real sqlite3 cursor and returns rows that DON'T support
        ``row["col_name"]`` access at all — only positional. This mimics
        the libsql shape that surfaced in production: the cursor wrapper
        couldn't resolve the column name ``key`` against ``description``,
        so name-keyed access raised ``IndexError``.
        """

        def __init__(self, inner):
            self._inner = inner

        def fetchall(self):
            return [tuple(r) for r in self._inner.fetchall()]

        def fetchone(self):
            r = self._inner.fetchone()
            return None if r is None else tuple(r)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class _NamelessConn:
        """Connection proxy: forwards everything to the real sqlite3
        connection EXCEPT ``execute`` for the two read paths that hit
        the ``settings`` table — those return a ``_NamelessCursor``
        whose rows are plain tuples, exactly the production shape.
        """

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            cur = self._inner.execute(sql, params) if params else self._inner.execute(sql)
            upper = sql.strip().upper()
            if upper.startswith("SELECT KEY, VALUE FROM SETTINGS") or \
               upper.startswith("SELECT VALUE FROM SETTINGS"):
                return _NamelessCursor(cur)
            return cur

        def __getattr__(self, name):
            return getattr(self._inner, name)

    store._conn = _NamelessConn(real_conn)    # type: ignore[assignment]
    # Bust the cache so the next get() actually re-reads from the DB
    # via the wrapped execute.
    store._cache = {}
    store._cache_loaded_at = 0.0

    try:
        # _load_sync hits the dict comprehension over the wrapped cursor.
        # If the production code regresses to ``row["key"]`` this raises
        # IndexError. With positional indexing it succeeds.
        value = await store.get("kie_model")
        assert value == "anchor-value"

        # _get_sync hits a single-column SELECT through the same path.
        direct = store._get_sync("kie_model")
        assert direct == "anchor-value"
    finally:
        store._conn = real_conn    # type: ignore[assignment]


async def test_get_returns_explicit_default_when_provided(store: SettingsStore) -> None:
    assert await store.get("unknown_key", default="fallback") == "fallback"


async def test_get_returns_empty_for_unregistered_unset_key(store: SettingsStore) -> None:
    assert await store.get("no_such_key") == ""


async def test_set_persists_value(store: SettingsStore) -> None:
    old = await store.set("script_system_prompt", "new-value", updated_by="yoav")
    assert old is None      # first write
    assert await store.get("script_system_prompt") == "new-value"


async def test_set_returns_old_value_on_update(store: SettingsStore) -> None:
    await store.set("script_system_prompt", "v1", updated_by="yoav")
    old = await store.set("script_system_prompt", "v2", updated_by="yoav")
    assert old == "v1"
    assert await store.get("script_system_prompt") == "v2"


async def test_get_all_merges_overrides_onto_defaults(store: SettingsStore) -> None:
    await store.set("script_system_prompt", "custom", updated_by="yoav")
    all_settings = await store.get_all()
    assert all_settings["script_system_prompt"] == "custom"
    assert all_settings["kie_model"] == "nano-banana"   # still default


async def test_audit_log_records_every_change(store: SettingsStore) -> None:
    await store.set("script_system_prompt", "v1", updated_by="yoav")
    await store.set("script_system_prompt", "v2", updated_by="yoav")
    await store.set("script_system_prompt", "v3", updated_by="alice")

    entries = await store.audit(key="script_system_prompt")
    assert len(entries) == 3
    # Most recent first.
    assert entries[0]["new_value"] == "v3"
    assert entries[0]["old_value"] == "v2"
    assert entries[0]["updated_by"] == "alice"
    assert entries[1]["new_value"] == "v2"
    assert entries[2]["new_value"] == "v1"
    assert entries[2]["old_value"] is None    # first write


async def test_audit_log_filter_by_key(store: SettingsStore) -> None:
    await store.set("script_system_prompt", "v1", updated_by="yoav")
    await store.set("kie_model", "midjourney", updated_by="yoav")

    prompt_entries = await store.audit(key="script_system_prompt")
    assert all(e["key"] == "script_system_prompt" for e in prompt_entries)
    assert len(prompt_entries) == 1


async def test_no_audit_row_when_value_is_unchanged(store: SettingsStore) -> None:
    await store.set("script_system_prompt", "same", updated_by="yoav")
    await store.set("script_system_prompt", "same", updated_by="yoav")
    entries = await store.audit(key="script_system_prompt")
    assert len(entries) == 1


async def test_persistence_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "settings.db"
    s1 = SettingsStore(path, defaults={"k": "d"}, cache_ttl_seconds=0.0)
    try:
        await s1.set("k", "saved", updated_by="yoav")
    finally:
        s1.close()

    s2 = SettingsStore(path, defaults={"k": "d"}, cache_ttl_seconds=0.0)
    try:
        assert await s2.get("k") == "saved"
    finally:
        s2.close()


async def test_defaults_method_returns_copy(store: SettingsStore) -> None:
    d = store.defaults()
    d["k"] = "leaked"      # mutate the returned dict
    # The store's internal defaults are unaffected.
    fresh = store.defaults()
    assert "k" not in fresh
