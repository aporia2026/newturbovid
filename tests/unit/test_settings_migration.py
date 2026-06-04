"""Tests for the legacy-key migration in SettingsStore.

Covers the one-shot ``script_system_prompt`` -> (``simple_script_prompt``,
``simple_x4_script_prompt``) copy that runs on every boot so deployed
customizations from before 2026-06-04 survive the per-tab prompt split.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bulkvid.orchestrator.runtime_settings import (
    SETTING_SCRIPT_SYSTEM_PROMPT,
    SETTING_SIMPLE_SCRIPT_PROMPT,
    SETTING_SIMPLE_X4_SCRIPT_PROMPT,
)
from bulkvid.orchestrator.settings_store import SettingsStore


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    s = SettingsStore(tmp_path / "settings.db", cache_ttl_seconds=0.0)
    yield s
    s.close()


_MAPPING = {
    SETTING_SCRIPT_SYSTEM_PROMPT: (
        SETTING_SIMPLE_SCRIPT_PROMPT,
        SETTING_SIMPLE_X4_SCRIPT_PROMPT,
    ),
}


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_migration_copies_legacy_to_both_new_keys(
    store: SettingsStore,
) -> None:
    await store.set(SETTING_SCRIPT_SYSTEM_PROMPT, "CUSTOM PROMPT", updated_by="yoav")

    written = store.migrate_legacy_keys_sync(_MAPPING)

    assert SETTING_SIMPLE_SCRIPT_PROMPT in written[SETTING_SCRIPT_SYSTEM_PROMPT]
    assert SETTING_SIMPLE_X4_SCRIPT_PROMPT in written[SETTING_SCRIPT_SYSTEM_PROMPT]
    assert await store.get(SETTING_SIMPLE_SCRIPT_PROMPT) == "CUSTOM PROMPT"
    assert await store.get(SETTING_SIMPLE_X4_SCRIPT_PROMPT) == "CUSTOM PROMPT"


async def test_migration_no_op_when_legacy_key_absent(
    store: SettingsStore,
) -> None:
    written = store.migrate_legacy_keys_sync(_MAPPING)
    assert written == {}


async def test_migration_does_not_overwrite_already_customized_new_key(
    store: SettingsStore,
) -> None:
    await store.set(SETTING_SCRIPT_SYSTEM_PROMPT, "LEGACY", updated_by="yoav")
    # Admin has already customized one of the new keys after the deploy.
    await store.set(
        SETTING_SIMPLE_SCRIPT_PROMPT, "NEW CUSTOMIZATION", updated_by="yoav"
    )

    written = store.migrate_legacy_keys_sync(_MAPPING)

    # Only the still-empty new key got the legacy value.
    assert written[SETTING_SCRIPT_SYSTEM_PROMPT] == [SETTING_SIMPLE_X4_SCRIPT_PROMPT]
    assert await store.get(SETTING_SIMPLE_SCRIPT_PROMPT) == "NEW CUSTOMIZATION"
    assert await store.get(SETTING_SIMPLE_X4_SCRIPT_PROMPT) == "LEGACY"


async def test_migration_is_idempotent_on_second_call(
    store: SettingsStore,
) -> None:
    await store.set(SETTING_SCRIPT_SYSTEM_PROMPT, "LEGACY", updated_by="yoav")

    first = store.migrate_legacy_keys_sync(_MAPPING)
    second = store.migrate_legacy_keys_sync(_MAPPING)

    assert len(first[SETTING_SCRIPT_SYSTEM_PROMPT]) == 2
    # Second run finds the new keys already populated and skips them.
    assert second == {}


async def test_migration_preserves_legacy_row_for_rollback(
    store: SettingsStore,
) -> None:
    await store.set(SETTING_SCRIPT_SYSTEM_PROMPT, "LEGACY", updated_by="yoav")

    store.migrate_legacy_keys_sync(_MAPPING)

    # Legacy key is still readable from the store so we could roll back the
    # deploy and resume reading it without losing the data.
    assert await store.get(SETTING_SCRIPT_SYSTEM_PROMPT) == "LEGACY"


async def test_migration_persists_across_store_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.db"

    s1 = SettingsStore(path, cache_ttl_seconds=0.0)
    try:
        await s1.set(SETTING_SCRIPT_SYSTEM_PROMPT, "FROZEN", updated_by="yoav")
        s1.migrate_legacy_keys_sync(_MAPPING)
    finally:
        s1.close()

    s2 = SettingsStore(path, cache_ttl_seconds=0.0)
    try:
        assert await s2.get(SETTING_SIMPLE_SCRIPT_PROMPT) == "FROZEN"
        assert await s2.get(SETTING_SIMPLE_X4_SCRIPT_PROMPT) == "FROZEN"
    finally:
        s2.close()


async def test_migration_writes_audit_log_entries(
    store: SettingsStore,
) -> None:
    await store.set(SETTING_SCRIPT_SYSTEM_PROMPT, "LEGACY", updated_by="yoav")
    store.migrate_legacy_keys_sync(_MAPPING)

    new_key_audit = await store.audit(key=SETTING_SIMPLE_SCRIPT_PROMPT)
    assert len(new_key_audit) == 1
    assert new_key_audit[0]["new_value"] == "LEGACY"
    assert new_key_audit[0]["updated_by"] == "migration"
