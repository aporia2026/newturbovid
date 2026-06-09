"""Tests for the avatar catalog (read-through cache for TikTok auto-fetch)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bulkvid.orchestrator.settings_store import SettingsStore
from bulkvid.pipeline.avatar_catalog import (
    SETTING_TIKTOK_AVATAR_CATALOG,
    load_catalog,
    replace_catalog,
)


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    s = SettingsStore(tmp_path / "settings.db", cache_ttl_seconds=0.0)
    yield s
    s.close()


# ── load_catalog ────────────────────────────────────────────────────────────


async def test_load_catalog_empty_initially(store: SettingsStore) -> None:
    assert await load_catalog(store) == []


async def test_load_catalog_recovers_from_garbage_value(
    store: SettingsStore,
) -> None:
    """A garbage value (non-JSON, or JSON that's not a list) must not
    crash — a future ``replace_catalog`` overwrites it cleanly."""
    await store.set(SETTING_TIKTOK_AVATAR_CATALOG, "not json", updated_by="t")
    assert await load_catalog(store) == []
    await store.set(
        SETTING_TIKTOK_AVATAR_CATALOG, '{"key": "value"}', updated_by="t",
    )
    assert await load_catalog(store) == []


# ── replace_catalog ─────────────────────────────────────────────────────────


async def test_replace_catalog_persists_valid_entries(
    store: SettingsStore,
) -> None:
    await replace_catalog(
        store,
        [
            {
                "avatar_id": "av-1",
                "name": "Anna",
                "gender": "FEMALE",    # normalized to lowercase
                "preview_url": "https://t.test/anna.png",
            },
            {
                "avatar_id": "av-2",
                "name": "Ben",
                "gender": "male",
                "preview_url": "https://t.test/ben.png",
            },
        ],
    )
    entries = await load_catalog(store)
    assert [e.avatar_id for e in entries] == ["av-1", "av-2"]
    assert entries[0].gender == "female"


async def test_replace_catalog_overwrites_previous_contents(
    store: SettingsStore,
) -> None:
    await replace_catalog(
        store, [{"avatar_id": "av-1", "name": "A"}]
    )
    await replace_catalog(
        store,
        [
            {"avatar_id": "av-2", "name": "B"},
            {"avatar_id": "av-3", "name": "C"},
        ],
    )
    entries = await load_catalog(store)
    assert [e.avatar_id for e in entries] == ["av-2", "av-3"]


async def test_replace_catalog_silently_drops_invalid_entries(
    store: SettingsStore,
) -> None:
    """A bad TikTok payload mustn't corrupt the cache — invalid entries
    are dropped, valid ones persist."""
    await replace_catalog(
        store,
        [
            {"avatar_id": "", "name": "no id"},                # dropped
            {"avatar_id": "av/1; DROP", "name": "bad chars"},   # dropped
            {"avatar_id": "av-good", "name": "ok"},             # kept
            {"avatar_id": "x" * 100, "name": "too long"},       # dropped (>64 chars)
        ],
    )
    entries = await load_catalog(store)
    assert [e.avatar_id for e in entries] == ["av-good"]
