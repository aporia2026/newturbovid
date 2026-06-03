"""Tests for the /jobs routes.

We assemble a FastAPI test app with:
  - A real JobQueue backed by tmp_path SQLite
  - A FAKE verifier that maps known bearer tokens to fixed Identities

Covers:
  - POST /jobs without Authorization -> 401
  - POST /jobs with invalid token -> 401
  - POST /jobs with valid token but unknown user -> 403
  - POST /jobs (image_vo) happy path -> 200 + job_id, row queued
  - POST /jobs (four_images_vo2) happy path -> 200
  - POST /jobs with missing rows for tab -> 400
  - POST /jobs with unknown tab_type -> 400
  - GET /jobs/{id} requires ownership (or admin)
  - GET /jobs returns bulk user's own jobs only
  - GET /jobs as admin returns ALL jobs across users
  - POST /jobs/{id}/kill works for owner, 403 for non-owner, 404 for unknown
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bulkvid.auth import AuthError, ForbiddenError, Identity
from bulkvid.orchestrator.queue import (
    JOB_QUEUED,
    TAB_FOUR_IMAGES,
    TAB_IMAGE_VO,
    TAB_SIMPLE,
    JobQueue,
)
from bulkvid.routes import jobs as jobs_routes

# ── Fake verifier ───────────────────────────────────────────────────────────


class _FakeVerifier:
    """Maps fixed bearer tokens to Identity objects."""

    TOKENS = {
        "tok-bulk1": Identity(
            email="bulk1@aporia.com", hd="aporia.com", name="Bulk One", is_admin=False
        ),
        "tok-bulk2": Identity(
            email="bulk2@aporia.com", hd="aporia.com", name="Bulk Two", is_admin=False
        ),
        "tok-admin": Identity(
            email="yoav@aporia.com", hd="aporia.com", name="Yoav", is_admin=True
        ),
        "tok-stranger": None,    # valid signature, NOT in allowlist
    }

    async def verify(self, bearer_token: str) -> Identity:
        if bearer_token == "tok-stranger":
            raise ForbiddenError("not on allowlist")
        if bearer_token not in self.TOKENS:
            raise AuthError("unknown test token")
        identity = self.TOKENS[bearer_token]
        assert identity is not None
        return identity


# ── App fixture ─────────────────────────────────────────────────────────────


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    a = FastAPI()
    a.include_router(jobs_routes.router)
    a.state.queue = JobQueue(tmp_path / "jobs.db")
    a.state.verifier = _FakeVerifier()
    yield a
    a.state.queue.close()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _image_vo_payload() -> dict:
    return {
        "sheet_id": "sheet-A",
        "worksheet": "Image-VO",
        "tab_type": TAB_IMAGE_VO,
        "rows_image_vo": [
            {
                "row_num": 2,
                "country": "US",
                "vertical": "tech",
                "article_url": "https://example.com/article",
                "manual_image_url": "https://example.com/seed.png",
                "voice_over": True,
                "zapcap": False,
                "aspect_ratio": "9:16",
                "script_pattern": "How To",
                "open_comments": "",
            }
        ],
    }


def _four_images_payload() -> dict:
    return {
        "sheet_id": "sheet-B",
        "worksheet": "4Images-VO2",
        "tab_type": TAB_FOUR_IMAGES,
        "rows_four_images": [
            {
                "row_num": 2,
                "country": "US",
                "vertical": "fashion",
                "article_url": "https://example.com/a",
                "how_many": 2,
                "voice_over": True,
                "image_urls": [
                    "https://example.com/i1.jpg",
                    "https://example.com/i2.jpg",
                ],
                "zapcap": True,
                "aspect_ratio": "1:1",
                "script_pattern": "Reveal",
                "open_comments": "urgent, mention $9.99",
            }
        ],
    }


def _simple_payload() -> dict:
    return {
        "sheet_id": "sheet-S",
        "worksheet": "simple",
        "tab_type": TAB_SIMPLE,
        "rows_simple": [
            {
                "row_num": 2,
                "country": "MX",
                "vertical": "automotive",
                "article_url": "https://example.com/article",
                "manual_image_url": "https://example.com/ad.png",
                "voice_over": True,
                "zapcap": False,
                "aspect_ratio": "9:16",
                "script_pattern": "How To",
                "open_comments": "",
            }
        ],
    }


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── Auth gating ─────────────────────────────────────────────────────────────


def test_post_jobs_without_authorization_returns_401(client: TestClient) -> None:
    r = client.post("/jobs", json=_image_vo_payload())
    assert r.status_code == 401


def test_post_jobs_with_malformed_authorization_returns_401(client: TestClient) -> None:
    r = client.post("/jobs", json=_image_vo_payload(), headers={"Authorization": "Token x"})
    assert r.status_code == 401


def test_post_jobs_with_invalid_token_returns_401(client: TestClient) -> None:
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bogus"))
    assert r.status_code == 401


def test_post_jobs_with_stranger_token_returns_403(client: TestClient) -> None:
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-stranger"))
    assert r.status_code == 403


# ── Submit happy paths ─────────────────────────────────────────────────────


def test_submit_image_vo_returns_job_id(client: TestClient) -> None:
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert body["row_count"] == 1
    assert body["job_id"].startswith("job-")


def test_submit_four_images_returns_job_id(client: TestClient) -> None:
    r = client.post("/jobs", json=_four_images_payload(), headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert body["row_count"] == 1


def test_submit_simple_returns_job_id(client: TestClient) -> None:
    r = client.post("/jobs", json=_simple_payload(), headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert body["row_count"] == 1


def test_submit_simple_without_rows_returns_400(client: TestClient) -> None:
    payload = _simple_payload()
    payload["rows_simple"] = []
    r = client.post("/jobs", json=payload, headers=_auth("tok-bulk1"))
    assert r.status_code == 400


def test_submit_image_vo_without_rows_returns_400(client: TestClient) -> None:
    payload = _image_vo_payload()
    payload["rows_image_vo"] = []
    r = client.post("/jobs", json=payload, headers=_auth("tok-bulk1"))
    assert r.status_code == 400


def test_submit_unknown_tab_type_returns_400(client: TestClient) -> None:
    payload = _image_vo_payload()
    payload["tab_type"] = "unknown_tab"
    r = client.post("/jobs", json=payload, headers=_auth("tok-bulk1"))
    assert r.status_code == 400


def test_submit_image_vo_payload_for_four_images_tab_returns_400(
    client: TestClient,
) -> None:
    payload = _four_images_payload()
    payload["rows_four_images"] = None
    r = client.post("/jobs", json=payload, headers=_auth("tok-bulk1"))
    assert r.status_code == 400


# ── GET /jobs/{id} ──────────────────────────────────────────────────────────


def test_get_job_returns_status(client: TestClient) -> None:
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    job_id = r.json()["job_id"]

    r = client.get(f"/jobs/{job_id}", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job_id
    assert body["status"] == JOB_QUEUED
    assert body["user_email"] == "bulk1@aporia.com"
    assert body["row_count"] == 1


def test_get_job_unknown_id_returns_404(client: TestClient) -> None:
    r = client.get("/jobs/job-bogus", headers=_auth("tok-bulk1"))
    assert r.status_code == 404


def test_get_job_returns_403_for_non_owner(client: TestClient) -> None:
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    job_id = r.json()["job_id"]
    r = client.get(f"/jobs/{job_id}", headers=_auth("tok-bulk2"))
    assert r.status_code == 403


def test_admin_can_see_any_job(client: TestClient) -> None:
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    job_id = r.json()["job_id"]
    r = client.get(f"/jobs/{job_id}", headers=_auth("tok-admin"))
    assert r.status_code == 200
    assert r.json()["user_email"] == "bulk1@aporia.com"


# ── GET /jobs (list) ────────────────────────────────────────────────────────


def test_list_jobs_returns_only_user_own(client: TestClient) -> None:
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk2"))

    r = client.get("/jobs", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) == 1
    assert jobs[0]["user_email"] == "bulk1@aporia.com"


def test_list_jobs_as_admin_returns_all(client: TestClient) -> None:
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk2"))

    r = client.get("/jobs", headers=_auth("tok-admin"))
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) == 2
    assert {j["user_email"] for j in jobs} == {"bulk1@aporia.com", "bulk2@aporia.com"}


# ── POST /jobs/{id}/kill ────────────────────────────────────────────────────


def test_kill_job_by_owner_succeeds(client: TestClient) -> None:
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    job_id = r.json()["job_id"]

    r = client.post(f"/jobs/{job_id}/kill", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job_id
    assert body["killed"] is True


def test_kill_job_by_non_owner_returns_403(client: TestClient) -> None:
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    job_id = r.json()["job_id"]
    r = client.post(f"/jobs/{job_id}/kill", headers=_auth("tok-bulk2"))
    assert r.status_code == 403


def test_kill_unknown_job_returns_404(client: TestClient) -> None:
    r = client.post("/jobs/job-bogus/kill", headers=_auth("tok-bulk1"))
    assert r.status_code == 404


def test_admin_can_kill_any_job(client: TestClient) -> None:
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    job_id = r.json()["job_id"]
    r = client.post(f"/jobs/{job_id}/kill", headers=_auth("tok-admin"))
    assert r.status_code == 200
    assert r.json()["killed"] is True


# ── GET /jobs/{id}/rows ─────────────────────────────────────────────────────


def test_get_job_rows_returns_per_row_status(client: TestClient) -> None:
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    job_id = r.json()["job_id"]

    r = client.get(f"/jobs/{job_id}/rows", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job_id
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["row_num"] == 2
    assert row["status"] == "pending"        # freshly queued, not yet claimed
    assert row["video_urls"] == []
    assert row["error"] is None


def test_get_job_rows_unknown_id_returns_404(client: TestClient) -> None:
    r = client.get("/jobs/job-bogus/rows", headers=_auth("tok-bulk1"))
    assert r.status_code == 404


def test_get_job_rows_non_owner_returns_403(client: TestClient) -> None:
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    job_id = r.json()["job_id"]
    r = client.get(f"/jobs/{job_id}/rows", headers=_auth("tok-bulk2"))
    assert r.status_code == 403


# ── GET /jobs/{id}/log ──────────────────────────────────────────────────────


def test_get_job_log_returns_empty_when_no_file(client: TestClient) -> None:
    # No logging handler runs in this test app, so the per-job file never exists.
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    job_id = r.json()["job_id"]

    r = client.get(f"/jobs/{job_id}/log", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job_id
    assert body["exists"] is False
    assert body["lines"] == []


def test_get_job_log_non_owner_returns_403(client: TestClient) -> None:
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    job_id = r.json()["job_id"]
    r = client.get(f"/jobs/{job_id}/log", headers=_auth("tok-bulk2"))
    assert r.status_code == 403


def test_get_job_log_unknown_id_returns_404(client: TestClient) -> None:
    r = client.get("/jobs/job-bogus/log", headers=_auth("tok-bulk1"))
    assert r.status_code == 404


# ── POST /jobs/kill-all ─────────────────────────────────────────────────────


def test_kill_all_clears_callers_queue(client: TestClient) -> None:
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    r = client.post("/jobs/kill-all", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    assert r.json()["killed"] == 1


def test_kill_all_is_scoped_to_caller_for_bulk_user(client: TestClient) -> None:
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    # bulk2 clears their own (empty) queue — bulk1's job is untouched.
    r = client.post("/jobs/kill-all", headers=_auth("tok-bulk2"))
    assert r.status_code == 200
    assert r.json()["killed"] == 0


def test_kill_all_as_admin_kills_everyones(client: TestClient) -> None:
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    r = client.post("/jobs/kill-all", headers=_auth("tok-admin"))
    assert r.status_code == 200
    assert r.json()["killed"] >= 1
