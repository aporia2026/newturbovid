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
