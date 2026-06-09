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
    connection (``_LibsqlConn``) does NOT implement the context-manager
    protocol, so the previous ``with self._conn:`` block raised
    ``TypeError: '_LibsqlConn' object does not support the context
    manager protocol``. The picker page tripped on this when it tried
    to cache the auto-fetched avatar list.

    Wrap the real sqlite3 connection in a proxy that hides ``__enter__``
    / ``__exit__``, then confirm ``set`` still writes via the
    autocommit fallback path."""
    real_conn = store._conn

    class _NoContextManagerProxy:
        """Forwards attribute access EXCEPT for the context-manager
        dunders. Mimics what httpx's libsql wrapper actually does."""
        def __init__(self, inner):
            self.__dict__["_inner"] = inner

        def __getattr__(self, name):
            if name in ("__enter__", "__exit__"):
                raise AttributeError(name)
            return getattr(self._inner, name)

        def __setattr__(self, name, value):
            setattr(self._inner, name, value)

    store._conn = _NoContextManagerProxy(real_conn)    # type: ignore[assignment]
    try:
        # Smoke test: set should succeed via the nullcontext fallback.
        old = await store.set(
            "kie_model", "first-write", updated_by="regression-test",
        )
        assert old is None    # never written before
        # Round-trip: the value reads back correctly.
        assert await store.get("kie_model") == "first-write"
        # Idempotent write: same value returns the old value, no error.
        old = await store.set(
            "kie_model", "first-write", updated_by="regression-test",
        )
        assert old == "first-write"
        # Updating to a new value also works through the fallback path.
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
