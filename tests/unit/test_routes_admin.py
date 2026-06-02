"""Tests for the admin panel routes.

The admin panel uses HTTP Basic auth (separate from the OAuth flow used by
the /jobs API).

Covers:
  - /admin/ without credentials -> 401
  - /admin/ with wrong credentials -> 401
  - /admin/ when ADMIN_PANEL_USERNAME/PASSWORD are unset -> 503
  - /admin/ with correct creds -> 200, HTML dashboard listing jobs
  - /admin/jobs/{id} renders job detail
  - /admin/jobs/{id} for unknown id -> 404
  - POST /admin/jobs/{id}/kill kills + returns updated status badge
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bulkvid.config import Settings, get_settings
from bulkvid.models.row import ImageVORow
from bulkvid.orchestrator.queue import (
    JOB_KILLED,
    JOB_QUEUED,
    TAB_IMAGE_VO,
    JobQueue,
)
from bulkvid.routes import admin as admin_routes


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override admin credentials for tests."""
    get_settings.cache_clear()

    def _fake_settings() -> Settings:
        return Settings(
            ADMIN_PANEL_USERNAME="admin",
            ADMIN_PANEL_PASSWORD="s3cret",
        )

    monkeypatch.setattr("bulkvid.routes.admin.get_settings", _fake_settings)


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    a = FastAPI()
    a.include_router(admin_routes.router)
    a.state.queue = JobQueue(tmp_path / "jobs.db")
    yield a
    a.state.queue.close()
    get_settings.cache_clear()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _basic(user: str, pw: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _img_row(n: int) -> ImageVORow:
    return ImageVORow(
        row_num=n,
        country="US",
        vertical="tech",
        article_url="https://example.com/a",
        manual_image_url="https://example.com/s.png",
        voice_over=True,
        zapcap=False,
        aspect_ratio="9:16",
        script_pattern="How To",
        open_comments="",
    )


async def _seed_one_job(app: FastAPI) -> str:
    return await app.state.queue.enqueue(
        user_email="bulk1@aporia.com",
        sheet_id="sheet-A",
        worksheet="Image-VO",
        tab_type=TAB_IMAGE_VO,
        rows=[_img_row(2), _img_row(3)],
    )


# ── Auth ────────────────────────────────────────────────────────────────────


def test_dashboard_without_auth_returns_401(client: TestClient) -> None:
    r = client.get("/admin/")
    assert r.status_code == 401


def test_dashboard_with_wrong_password_returns_401(client: TestClient) -> None:
    r = client.get("/admin/", headers=_basic("admin", "wrong"))
    assert r.status_code == 401


def test_dashboard_with_wrong_username_returns_401(client: TestClient) -> None:
    r = client.get("/admin/", headers=_basic("attacker", "s3cret"))
    assert r.status_code == 401


def test_dashboard_returns_503_when_credentials_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_settings.cache_clear()

    def _fake_settings() -> Settings:
        # Both empty.
        return Settings(ADMIN_PANEL_USERNAME="", ADMIN_PANEL_PASSWORD="")

    monkeypatch.setattr("bulkvid.routes.admin.get_settings", _fake_settings)
    a = FastAPI()
    a.include_router(admin_routes.router)
    a.state.queue = JobQueue(tmp_path / "jobs.db")
    try:
        with TestClient(a) as c:
            r = c.get("/admin/", headers=_basic("admin", "s3cret"))
        assert r.status_code == 503
    finally:
        a.state.queue.close()


# ── Dashboard ──────────────────────────────────────────────────────────────


def test_dashboard_with_correct_auth_returns_html(
    client: TestClient, app: FastAPI
) -> None:
    import asyncio

    asyncio.run(_seed_one_job(app))

    r = client.get("/admin/", headers=_basic("admin", "s3cret"))
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    # Header chrome.
    assert "TurboVid Admin" in body
    # Job row.
    assert "bulk1@aporia.com" in body
    assert "image_vo" in body


def test_empty_dashboard_renders_empty_state(client: TestClient) -> None:
    r = client.get("/admin/", headers=_basic("admin", "s3cret"))
    assert r.status_code == 200
    assert "No jobs yet" in r.text


# ── Job detail ─────────────────────────────────────────────────────────────


def test_job_detail_renders(client: TestClient, app: FastAPI) -> None:
    import asyncio

    job_id = asyncio.run(_seed_one_job(app))

    r = client.get(f"/admin/jobs/{job_id}", headers=_basic("admin", "s3cret"))
    assert r.status_code == 200
    body = r.text
    assert job_id in body
    assert "bulk1@aporia.com" in body
    assert "sheet-A" in body
    # Status badge.
    assert "queued" in body.lower()


def test_job_detail_unknown_id_returns_404(client: TestClient) -> None:
    r = client.get("/admin/jobs/job-bogus", headers=_basic("admin", "s3cret"))
    assert r.status_code == 404


# ── Kill ───────────────────────────────────────────────────────────────────


def test_kill_returns_updated_status_badge(client: TestClient, app: FastAPI) -> None:
    import asyncio

    job_id = asyncio.run(_seed_one_job(app))

    r = client.post(f"/admin/jobs/{job_id}/kill", headers=_basic("admin", "s3cret"))
    assert r.status_code == 200
    # HTMX swap target is just the badge fragment.
    assert "killed" in r.text.lower()
    # And the queue state actually changed.
    job = asyncio.run(app.state.queue.get_job(job_id))
    assert job is not None
    assert job.status == JOB_KILLED


def test_kill_unknown_job_returns_404(client: TestClient) -> None:
    r = client.post("/admin/jobs/job-bogus/kill", headers=_basic("admin", "s3cret"))
    assert r.status_code == 404
