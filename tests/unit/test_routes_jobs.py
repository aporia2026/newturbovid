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
    TAB_CARTOON,
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


def _cartoon_payload() -> dict:
    return {
        "sheet_id": "sheet-C",
        "worksheet": "cartoon",
        "tab_type": TAB_CARTOON,
        "rows_cartoon": [
            {
                "row_num": 2,
                "country": "MX",
                "vertical": "automotive",
                "article_url": "https://example.com/article",
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


def test_submit_cartoon_returns_job_id(client: TestClient) -> None:
    r = client.post("/jobs", json=_cartoon_payload(), headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert body["row_count"] == 1


def test_submit_cartoon_without_rows_returns_400(client: TestClient) -> None:
    payload = _cartoon_payload()
    payload["rows_cartoon"] = []
    r = client.post("/jobs", json=payload, headers=_auth("tok-bulk1"))
    assert r.status_code == 400


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


# ── GET /jobs/poll ──────────────────────────────────────────────────────────
# The batched-poll endpoint the sidebar uses on every cycle. Replaces three
# separate authenticated requests with one. Plan:
# _plans/2026-06-04-fix-sidebar-500s.md.


def test_poll_without_auth_returns_401(client: TestClient) -> None:
    r = client.get("/jobs/poll")
    assert r.status_code == 401


def test_poll_owner_returns_only_own_jobs(client: TestClient) -> None:
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk2"))

    r = client.get("/jobs/poll", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["user_email"] == "bulk1@aporia.com"
    assert body["rows_by_job"] == {}            # job is queued, not running
    assert body["logs_by_job"] == {}


def test_poll_as_admin_returns_all_jobs(client: TestClient) -> None:
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk2"))

    r = client.get("/jobs/poll", headers=_auth("tok-admin"))
    assert r.status_code == 200
    body = r.json()
    assert len(body["jobs"]) == 2
    assert {j["user_email"] for j in body["jobs"]} == {
        "bulk1@aporia.com",
        "bulk2@aporia.com",
    }


def test_poll_empty_state(client: TestClient) -> None:
    r = client.get("/jobs/poll", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert body == {"jobs": [], "rows_by_job": {}, "logs_by_job": {}}


def test_poll_rows_only_populated_for_running_jobs(
    client: TestClient, app: FastAPI
) -> None:
    """Queued jobs must NOT carry per-row detail in the poll response. That
    matches what the sidebar shows ("Waiting in queue…") and keeps the per-poll
    DB work proportional to the active set."""
    # Two jobs enqueued in order. FIFO claim takes the first one and promotes
    # it to RUNNING. The second one stays QUEUED until its turn.
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    first_id = r.json()["job_id"]
    r = client.post("/jobs", json=_four_images_payload(), headers=_auth("tok-bulk1"))
    second_id = r.json()["job_id"]

    claimed = await_(app.state.queue.claim_next_row())
    assert claimed is not None
    assert claimed.job_id == first_id            # FIFO

    r = client.get("/jobs/poll", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    # The running job carries its rows; the still-queued job does NOT.
    assert first_id in body["rows_by_job"]
    assert len(body["rows_by_job"][first_id]) >= 1
    assert second_id not in body["rows_by_job"]


def test_poll_logs_only_for_owned_and_requested_jobs(client: TestClient) -> None:
    """The ``logs=`` query param filters to a) jobs the caller owns AND b)
    job IDs the caller explicitly requested. A malicious caller naming another
    user's job ID gets nothing back — silently, not a 403, so we don't leak
    job existence."""
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    own_id = r.json()["job_id"]
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk2"))
    other_id = r.json()["job_id"]

    # Owner asks for their own log + someone else's log.
    r = client.get(
        f"/jobs/poll?logs={own_id},{other_id}",
        headers=_auth("tok-bulk1"),
    )
    assert r.status_code == 200
    body = r.json()
    # Own job's log entry is present (file doesn't exist in tests, but the key
    # is there with exists=False and lines=[]).
    assert own_id in body["logs_by_job"]
    assert body["logs_by_job"][own_id] == {"exists": False, "lines": []}
    # Other user's job is silently absent.
    assert other_id not in body["logs_by_job"]


def test_poll_log_tail_clamped(client: TestClient) -> None:
    """``log_tail`` cap matches ``read_job_log_lines`` (max 2000). Caller cannot
    request an unbounded log slice via the poll endpoint."""
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    jid = r.json()["job_id"]
    # log_tail well above the cap; endpoint should not error.
    r = client.get(
        f"/jobs/poll?logs={jid}&log_tail=999999",
        headers=_auth("tok-bulk1"),
    )
    assert r.status_code == 200


def test_poll_rejects_too_many_log_ids(client: TestClient) -> None:
    """Bounded fan-out: >50 log IDs in a single poll is rejected before any
    file I/O happens."""
    fake_ids = ",".join(f"job-{i}" for i in range(51))
    r = client.get(
        f"/jobs/poll?logs={fake_ids}",
        headers=_auth("tok-bulk1"),
    )
    assert r.status_code == 400


def test_poll_limit_clamped(client: TestClient) -> None:
    """``limit`` is clamped to [1, 500]. Caller cannot exhaust the DB via a
    massive single response."""
    r = client.get("/jobs/poll?limit=99999", headers=_auth("tok-bulk1"))
    assert r.status_code == 200            # accepted, just clamped


# ── /jobs/poll routing: 'poll' must not be matched as {job_id} ─────────────


def test_poll_path_not_swallowed_by_job_id_route(client: TestClient) -> None:
    """``GET /jobs/poll`` must hit the poll handler — not the
    ``GET /jobs/{job_id}`` route looking up a job called "poll". This guards
    against an accidental route-order regression that would 404 every poll."""
    r = client.get("/jobs/poll", headers=_auth("tok-bulk1"))
    # 200 (empty state) confirms poll handler hit, not 404 from "job 'poll'
    # not found".
    assert r.status_code == 200
    assert "jobs" in r.json()


# ── Helper: run a coroutine from a sync test ────────────────────────────────


def await_(coro):    # noqa: ANN001
    """Tiny adapter — these route tests are sync ``TestClient`` tests, but the
    JobQueue API is async. Drive the coroutine on the running loop or a new one.
    """
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    return loop.run_until_complete(coro)
