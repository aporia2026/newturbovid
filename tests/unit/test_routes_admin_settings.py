"""Tests for the admin settings UI routes."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bulkvid.config import Settings, get_settings
from bulkvid.orchestrator.queue import JobQueue
from bulkvid.orchestrator.runtime_settings import (
    SCRIPT_SYSTEM_PROMPT_DEFAULT,
    SETTING_SCRIPT_SYSTEM_PROMPT,
    registry_defaults,
)
from bulkvid.orchestrator.settings_store import SettingsStore
from bulkvid.routes import admin as admin_routes


@pytest.fixture(autouse=True)
def _patch_admin_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_settings() -> Settings:
        return Settings(
            ADMIN_PANEL_USERNAME="yoav",
            ADMIN_PANEL_PASSWORD="tenta20",
        )

    monkeypatch.setattr("bulkvid.routes.admin.get_settings", _fake_settings)


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    a = FastAPI()
    a.include_router(admin_routes.router)
    a.state.queue = JobQueue(tmp_path / "jobs.db")
    a.state.settings_store = SettingsStore(
        tmp_path / "settings.db",
        defaults=registry_defaults(),
        cache_ttl_seconds=0.0,
    )
    yield a
    a.state.queue.close()
    a.state.settings_store.close()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _basic(user: str, pw: str) -> dict[str, str]:
    return {"Authorization": f"Basic {base64.b64encode(f'{user}:{pw}'.encode()).decode()}"}


# ── List page ───────────────────────────────────────────────────────────────


def test_settings_list_without_auth_returns_401(client: TestClient) -> None:
    assert client.get("/admin/settings").status_code == 401


def test_settings_list_renders_registered_settings(client: TestClient) -> None:
    r = client.get("/admin/settings", headers=_basic("yoav", "tenta20"))
    assert r.status_code == 200
    body = r.text
    assert "Script generator system prompt" in body
    # The default value's preview shows somewhere in the table.
    assert "commercial or educational video" in body


# ── Detail page ─────────────────────────────────────────────────────────────


def test_settings_detail_renders_current_value(client: TestClient) -> None:
    r = client.get(
        f"/admin/settings/{SETTING_SCRIPT_SYSTEM_PROMPT}",
        headers=_basic("yoav", "tenta20"),
    )
    assert r.status_code == 200
    body = r.text
    assert "Script generator system prompt" in body
    # Some recognizable line from the default prompt.
    assert "Maximum length: 40 words" in body


def test_settings_detail_unknown_key_returns_404(client: TestClient) -> None:
    r = client.get("/admin/settings/nonexistent", headers=_basic("yoav", "tenta20"))
    assert r.status_code == 404


# ── Save (POST) ─────────────────────────────────────────────────────────────


def test_save_persists_new_value_and_redirects(
    client: TestClient, app: FastAPI
) -> None:
    new_value = "REPLACED PROMPT for testing"
    r = client.post(
        f"/admin/settings/{SETTING_SCRIPT_SYSTEM_PROMPT}",
        headers=_basic("yoav", "tenta20"),
        data={"value": new_value},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/admin/settings/{SETTING_SCRIPT_SYSTEM_PROMPT}"

    # Detail page now shows the new value.
    detail = client.get(
        f"/admin/settings/{SETTING_SCRIPT_SYSTEM_PROMPT}",
        headers=_basic("yoav", "tenta20"),
    )
    assert "REPLACED PROMPT for testing" in detail.text
    # And no longer marked as default.
    assert "currently using default" not in detail.text
    assert "customized" in detail.text


def test_save_unknown_key_returns_404(client: TestClient) -> None:
    r = client.post(
        "/admin/settings/nonexistent",
        headers=_basic("yoav", "tenta20"),
        data={"value": "x"},
    )
    assert r.status_code == 404


# ── Reset ───────────────────────────────────────────────────────────────────


def test_reset_restores_default(client: TestClient) -> None:
    auth = _basic("yoav", "tenta20")

    # First save a custom value.
    client.post(
        f"/admin/settings/{SETTING_SCRIPT_SYSTEM_PROMPT}",
        headers=auth, data={"value": "custom"},
        follow_redirects=False,
    )
    # Now reset.
    r = client.post(
        f"/admin/settings/{SETTING_SCRIPT_SYSTEM_PROMPT}/reset",
        headers=auth,
        follow_redirects=False,
    )
    assert r.status_code == 303

    detail = client.get(
        f"/admin/settings/{SETTING_SCRIPT_SYSTEM_PROMPT}", headers=auth
    )
    body = detail.text
    # The full default prompt is back.
    assert "Maximum length: 40 words" in body


# ── Auth gating + audit log visibility ──────────────────────────────────────


def test_save_without_auth_returns_401(client: TestClient) -> None:
    r = client.post(
        f"/admin/settings/{SETTING_SCRIPT_SYSTEM_PROMPT}",
        data={"value": "x"},
    )
    assert r.status_code == 401


def test_audit_log_shows_in_detail_page_after_edits(client: TestClient) -> None:
    auth = _basic("yoav", "tenta20")
    client.post(
        f"/admin/settings/{SETTING_SCRIPT_SYSTEM_PROMPT}",
        headers=auth, data={"value": "edit-one"},
        follow_redirects=False,
    )
    detail = client.get(
        f"/admin/settings/{SETTING_SCRIPT_SYSTEM_PROMPT}", headers=auth
    )
    body = detail.text
    assert "Change history" in body
    # Author of the edit shows up.
    assert "yoav" in body
