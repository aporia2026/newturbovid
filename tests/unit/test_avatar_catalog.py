"""Tests for the manual TikTok avatar catalog."""

from __future__ import annotations

from pathlib import Path

import pytest

from bulkvid.orchestrator.settings_store import SettingsStore
from bulkvid.pipeline.avatar_catalog import (
    SETTING_TIKTOK_AVATAR_CATALOG,
    add_avatar,
    delete_avatar,
    load_catalog,
)


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    s = SettingsStore(tmp_path / "settings.db", cache_ttl_seconds=0.0)
    yield s
    s.close()


# ── Empty / decode ───────────────────────────────────────────────────────────


async def test_load_catalog_empty_initially(store: SettingsStore) -> None:
    assert await load_catalog(store) == []


async def test_load_catalog_recovers_from_garbage_value(
    store: SettingsStore,
) -> None:
    """A garbage value (non-JSON, or JSON that's not a list) must not
    crash — the catalog falls back to empty so a future ``add_avatar``
    overwrites it cleanly."""
    await store.set(SETTING_TIKTOK_AVATAR_CATALOG, "not json", updated_by="t")
    assert await load_catalog(store) == []
    await store.set(SETTING_TIKTOK_AVATAR_CATALOG, '{"key": "value"}', updated_by="t")
    assert await load_catalog(store) == []


# ── Add ──────────────────────────────────────────────────────────────────────


async def test_add_avatar_appends_to_catalog(store: SettingsStore) -> None:
    err = await add_avatar(
        store,
        avatar_id="av-1",
        name="Anna",
        gender="female",
        preview_url="https://t.test/anna.png",
        notes="warm narrator",
    )
    assert err is None
    entries = await load_catalog(store)
    assert len(entries) == 1
    assert entries[0].avatar_id == "av-1"
    assert entries[0].name == "Anna"
    assert entries[0].gender == "female"
    assert entries[0].preview_url == "https://t.test/anna.png"
    assert entries[0].notes == "warm narrator"


async def test_add_avatar_normalizes_gender_case(store: SettingsStore) -> None:
    err = await add_avatar(
        store, avatar_id="av-1", name="", gender="FEMALE", preview_url="",
    )
    assert err is None
    entries = await load_catalog(store)
    assert entries[0].gender == "female"


async def test_add_avatar_with_same_id_upserts_in_place(
    store: SettingsStore,
) -> None:
    """Adding an ID that already exists replaces the existing entry
    rather than appending a duplicate — ordering stays stable."""
    await add_avatar(store, avatar_id="av-1", name="A", gender="", preview_url="")
    await add_avatar(store, avatar_id="av-2", name="B", gender="", preview_url="")
    await add_avatar(
        store, avatar_id="av-1", name="A2", gender="male",
        preview_url="https://t.test/a2.png",
    )
    entries = await load_catalog(store)
    assert [(e.avatar_id, e.name, e.gender) for e in entries] == [
        ("av-1", "A2", "male"),
        ("av-2", "B", ""),
    ]


# ── Validation ───────────────────────────────────────────────────────────────


async def test_add_avatar_rejects_empty_id(store: SettingsStore) -> None:
    err = await add_avatar(
        store, avatar_id="", name="x", gender="", preview_url="",
    )
    assert err == "avatar_id is required"
    assert await load_catalog(store) == []


async def test_add_avatar_rejects_id_with_special_chars(
    store: SettingsStore,
) -> None:
    err = await add_avatar(
        store, avatar_id="av/1; DROP TABLE", name="", gender="", preview_url="",
    )
    assert err is not None
    assert "alphanumeric" in err
    assert await load_catalog(store) == []


async def test_add_avatar_rejects_non_http_preview_url(
    store: SettingsStore,
) -> None:
    err = await add_avatar(
        store, avatar_id="av-1", name="", gender="",
        preview_url="javascript:alert(1)",
    )
    assert err is not None
    assert "http://" in err
    assert await load_catalog(store) == []


async def test_add_avatar_rejects_unknown_gender(store: SettingsStore) -> None:
    err = await add_avatar(
        store, avatar_id="av-1", name="", gender="alien", preview_url="",
    )
    assert err is not None
    assert "gender" in err


async def test_add_avatar_accepts_empty_preview_url(store: SettingsStore) -> None:
    """preview_url is optional — the page falls back to a placeholder."""
    err = await add_avatar(
        store, avatar_id="av-1", name="Anna", gender="female", preview_url="",
    )
    assert err is None


# ── Delete ───────────────────────────────────────────────────────────────────


async def test_delete_avatar_removes_matching_entry(store: SettingsStore) -> None:
    await add_avatar(store, avatar_id="av-1", name="A", gender="", preview_url="")
    await add_avatar(store, avatar_id="av-2", name="B", gender="", preview_url="")
    removed = await delete_avatar(store, avatar_id="av-1")
    assert removed is True
    entries = await load_catalog(store)
    assert [e.avatar_id for e in entries] == ["av-2"]


async def test_delete_avatar_returns_false_when_id_not_found(
    store: SettingsStore,
) -> None:
    await add_avatar(store, avatar_id="av-1", name="A", gender="", preview_url="")
    removed = await delete_avatar(store, avatar_id="nope")
    assert removed is False
    entries = await load_catalog(store)
    assert [e.avatar_id for e in entries] == ["av-1"]
