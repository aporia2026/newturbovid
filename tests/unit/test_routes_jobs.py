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
    TAB_SIMPLE_X4,
    JobQueue,
    QueueUnavailable,
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


def _simple_x4_payload(
    *,
    cards: list[dict] | None = None,
) -> dict:
    """Minimal valid simple_x4 submit body. ``cards`` defaults to all-blank
    (no template overlay)."""
    if cards is None:
        cards = [{"template_id": "", "cta": ""}] * 4
    return {
        "sheet_id": "sheet-X4",
        "worksheet": "simple x4",
        "tab_type": TAB_SIMPLE_X4,
        "rows_simple_x4": [
            {
                "row_num": 3,
                "country": "DE",
                "vertical": "Car Deals PR",
                "article_url": "https://example.com/article",
                "manual_image_url": "https://example.com/seed.png",
                "voice_over": True,
                "zapcap": False,
                "aspect_ratio": "9:16",
                "script_pattern": "",
                "cards": cards,
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
    # Fresh submit — nothing was dropped.
    assert body["dropped_count"] == 0
    assert body["submitted_count"] == 1
    assert body["job_id"].startswith("job-")


def test_submit_with_all_rows_deduped_returns_zero_kept_and_drop_count(
    client: TestClient,
) -> None:
    """Regression for chat 2026-06-09: when the queue dedup silently
    suppresses every row (all submitted row_nums are already pending or
    processing in another active job for the same sheet+worksheet),
    the route used to return ``row_count = len(rows)`` — making the
    response indistinguishable from a healthy submit. Apps Script then
    showed "Job submitted: N" while the job was actually 0/0.

    The fix surfaces the actual kept count + a dropped count + the
    original submitted count so the client can warn the operator.
    """
    # First submit lands the row in pending/queued state.
    first = client.post(
        "/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1")
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["row_count"] == 1
    assert first_body["dropped_count"] == 0

    # Resubmit the SAME row_num for the SAME sheet+worksheet. The dedup
    # guard in ``_enqueue_sync`` drops it; the resulting job has 0 rows.
    second = client.post(
        "/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1")
    )
    assert second.status_code == 200, second.text
    body = second.json()
    # Honest accounting: 0 kept, 1 dropped, 1 submitted, status reflects
    # that the job is already complete (0/0) rather than queued for work.
    assert body["row_count"] == 0
    assert body["dropped_count"] == 1
    assert body["submitted_count"] == 1
    assert body["status"] == "completed"
    # And the job_id is brand-new (the previous one is still active and
    # owns the row — we don't replay it).
    assert body["job_id"] != first_body["job_id"]


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


def test_submit_simple_x4_returns_job_id(client: TestClient) -> None:
    r = client.post("/jobs", json=_simple_x4_payload(), headers=_auth("tok-bulk1"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["row_count"] == 1


def test_submit_simple_x4_without_rows_returns_400(client: TestClient) -> None:
    payload = _simple_x4_payload()
    payload["rows_simple_x4"] = []
    r = client.post("/jobs", json=payload, headers=_auth("tok-bulk1"))
    assert r.status_code == 400


def test_simple_x4_route_payload_with_invalid_cards_still_returns_200(
    client: TestClient,
) -> None:
    """Garbage template_id + over-long CTA + missing cards must NOT 4xx —
    coercion in ``_build_simple_x4_row`` cleans them server-side."""
    payload = _simple_x4_payload(
        cards=[
            {"template_id": "9", "cta": "x" * 500},     # bad id + long CTA
            {"template_id": "maybe", "cta": "ok"},
        ]    # only 2 cards (Apps Script always sends 4 — defensive)
    )
    r = client.post("/jobs", json=payload, headers=_auth("tok-bulk1"))
    assert r.status_code == 200, r.text


def test_build_simple_x4_row_coerces_invalid_template_to_empty() -> None:
    """Direct unit test on the server-side coercion helper. Valid ids are
    "", "1", "2", "3" — any other value silently downgrades to "" so a
    typo doesn't fail the row."""
    from bulkvid.routes.jobs import CardChoiceIn, SimpleX4RowIn, _build_simple_x4_row

    r_in = SimpleX4RowIn(
        row_num=3,
        article_url="https://example.com/a",
        manual_image_url="https://example.com/i.png",
        cards=[
            CardChoiceIn(template_id="3", cta="Buy"),     # valid (since 2026-06-08)
            CardChoiceIn(template_id="maybe", cta="Look"),
            CardChoiceIn(template_id="1", cta="Click"),
            CardChoiceIn(template_id="9", cta=""),         # invalid → ""
        ],
    )
    row = _build_simple_x4_row(r_in)
    assert [c.template_id for c in row.cards] == ["3", "", "1", ""]
    assert [c.cta for c in row.cards] == ["Buy", "Look", "Click", ""]


def test_build_simple_x4_row_truncates_long_cta() -> None:
    from bulkvid.routes.jobs import CardChoiceIn, SimpleX4RowIn, _build_simple_x4_row

    r_in = SimpleX4RowIn(
        row_num=3,
        article_url="https://example.com/a",
        manual_image_url="https://example.com/i.png",
        cards=[CardChoiceIn(template_id="1", cta="x" * 500)],
    )
    row = _build_simple_x4_row(r_in)
    assert len(row.cards[0].cta) == 80


def test_build_simple_x4_row_pads_cards_to_four() -> None:
    """Apps Script always sends 4 entries; a hand-crafted payload with fewer
    must not blow up — pad with empty entries."""
    from bulkvid.routes.jobs import CardChoiceIn, SimpleX4RowIn, _build_simple_x4_row

    r_in = SimpleX4RowIn(
        row_num=3,
        article_url="https://example.com/a",
        manual_image_url="https://example.com/i.png",
        cards=[CardChoiceIn(template_id="1", cta="Go")],    # only 1
    )
    row = _build_simple_x4_row(r_in)
    assert len(row.cards) == 4
    assert row.cards[0].template_id == "1"
    assert row.cards[0].cta == "Go"
    assert all(c.template_id == "" and c.cta == "" for c in row.cards[1:])


def test_build_simple_x4_row_trims_cards_beyond_four() -> None:
    """A payload with MORE than 4 cards is truncated, never crashed."""
    from bulkvid.routes.jobs import CardChoiceIn, SimpleX4RowIn, _build_simple_x4_row

    r_in = SimpleX4RowIn(
        row_num=3,
        article_url="https://example.com/a",
        manual_image_url="https://example.com/i.png",
        cards=[CardChoiceIn(template_id="1", cta=f"c{i}") for i in range(7)],
    )
    row = _build_simple_x4_row(r_in)
    assert len(row.cards) == 4
    assert [c.cta for c in row.cards] == ["c0", "c1", "c2", "c3"]


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


def test_jobs_avatars_route_is_not_shadowed_by_job_id_param(
    client: TestClient,
) -> None:
    """Regression for chat 2026-06-09: ``GET /jobs/avatars`` returned
    404 ``job not found`` because the literal ``/avatars`` route was
    declared AFTER the dynamic ``/{job_id}`` route in jobs.py — FastAPI
    matched in order and treated ``avatars`` as a job_id.

    A non-404 (even an upstream TikTok error) proves the route is now
    reachable. We don't assert 200 because the test environment has no
    TikTok credentials, so the fetch path returns a 200 with
    source='empty' and an error string — not a 404."""
    r = client.get("/jobs/avatars", headers=_auth("tok-bulk1"))
    assert r.status_code != 404, (
        f"expected /jobs/avatars to be reachable, got {r.status_code}: {r.text[:200]}"
    )


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
    # ``eta_medians_by_tab`` was added in the Phase 3 sidebar UX
    # overhaul and is always present (empty when there are no
    # completed rows yet). Plan:
    # _plans/2026-06-04-sidebar-ux-overhaul.md §Phase 3.
    # ``queue_status`` was added 2026-06-09 for the sidebar's row-level
    # queue depth banner. Always present even on an empty queue so the
    # client can render "0 / N in flight" without a null-check.
    assert body == {
        "jobs": [],
        "rows_by_job": {},
        "logs_by_job": {},
        "eta_medians_by_tab": {},
        "queue_status": {
            "in_flight": 0,
            "queued": 0,
            # default ``BULKVID_MAX_CONCURRENT_ROWS`` from config.py.
            "max_concurrent": 10,
            # No queued rows → ETA collapses to 0 (not None) so the
            # banner can hide the "ETA" line on its own and not have to
            # conflate "no queue" with "no medians yet".
            "eta_seconds": 0,
            # No queued rows → stuck-detect short-circuits to None
            # (nothing to be stuck on). Sidebar hides the warning.
            "stuck_queued_seconds": None,
        },
    }


def test_poll_queue_status_counts_inflight_and_queued_for_user(
    client: TestClient, app: FastAPI,
) -> None:
    """Two image-vo jobs submitted; first row claimed (in-flight),
    second still pending. ``queue_status`` must report 1 in-flight + 1
    queued for the submitting user."""
    # Two single-row jobs.
    r1 = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    assert r1.status_code == 200
    r2 = client.post(
        "/jobs",
        json={**_image_vo_payload(), "worksheet": "Image-VO-2"},
        headers=_auth("tok-bulk1"),
    )
    assert r2.status_code == 200

    # Promote the first job's row to PROCESSING (the worker would do this).
    claimed = await_(app.state.queue.claim_next_row())
    assert claimed is not None

    r = client.get("/jobs/poll", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    qs = r.json()["queue_status"]
    assert qs["in_flight"] == 1
    assert qs["queued"] == 1
    # No medians yet (no completed rows) → eta_seconds is None, not 0,
    # so the client knows to hide the ETA line rather than show "0 min".
    assert qs["eta_seconds"] is None


def _image_vo_payload_for_row(row_num: int, *, worksheet: str = "Image-VO") -> dict:
    """Variant of ``_image_vo_payload`` that lets the caller pick a row /
    worksheet so cross-user tests don't trip the queue dedup guard
    (which collapses any (sheet, worksheet, row_num) already pending
    in an active job — see ``_enqueue_sync``)."""
    payload = _image_vo_payload()
    payload["worksheet"] = worksheet
    payload["rows_image_vo"][0]["row_num"] = row_num
    return payload


def test_poll_queue_status_isolated_per_user(
    client: TestClient, app: FastAPI,
) -> None:
    """Bulk user A's queued rows must NOT count toward bulk user B's
    ``queue_status``. Each non-admin caller sees only their own depth.

    Distinct ``worksheet`` per user so the queue dedup guard doesn't
    collapse B's row into A's pending one — same defensive shape as the
    real Apps Script payload (different sheets per operator)."""
    client.post(
        "/jobs",
        json=_image_vo_payload_for_row(2, worksheet="Bulk1-Sheet"),
        headers=_auth("tok-bulk1"),
    )
    await_(app.state.queue.claim_next_row())
    client.post(
        "/jobs",
        json=_image_vo_payload_for_row(2, worksheet="Bulk2-Sheet"),
        headers=_auth("tok-bulk2"),
    )

    r_a = client.get("/jobs/poll", headers=_auth("tok-bulk1")).json()
    r_b = client.get("/jobs/poll", headers=_auth("tok-bulk2")).json()

    # A sees their own 1 in-flight + 0 queued.
    assert r_a["queue_status"]["in_flight"] == 1
    assert r_a["queue_status"]["queued"] == 0
    # B sees their own 0 in-flight + 1 queued. NOT A's row.
    assert r_b["queue_status"]["in_flight"] == 0
    assert r_b["queue_status"]["queued"] == 1


def test_poll_queue_status_admin_sees_whole_fleet(
    client: TestClient, app: FastAPI,
) -> None:
    """Admin's ``queue_status`` aggregates rows across ALL users — same
    fleet view ``list_jobs`` already gives admins."""
    client.post(
        "/jobs",
        json=_image_vo_payload_for_row(2, worksheet="Bulk1-Sheet"),
        headers=_auth("tok-bulk1"),
    )
    client.post(
        "/jobs",
        json=_image_vo_payload_for_row(2, worksheet="Bulk2-Sheet"),
        headers=_auth("tok-bulk2"),
    )

    r = client.get("/jobs/poll", headers=_auth("tok-admin")).json()
    assert r["queue_status"]["in_flight"] == 0
    assert r["queue_status"]["queued"] == 2


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


# ── Idempotency on POST /jobs (rule 18) ─────────────────────────────────────
# Plan: _plans/2026-06-04-submit-500-defensive-fix.md §"Change 1".
# A submit POST that PA's frontend dropped on the way back to the Apps Script
# is retried with the SAME key — we must return the SAME job_id, no duplicate.


def test_submit_idempotency_replay_returns_same_job(client: TestClient) -> None:
    payload = _image_vo_payload()
    payload["idempotency_key"] = "sub-12345-abcdef"
    r1 = client.post("/jobs", json=payload, headers=_auth("tok-bulk1"))
    r2 = client.post("/jobs", json=payload, headers=_auth("tok-bulk1"))
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["job_id"] == r2.json()["job_id"]
    # Only ONE job was actually created.
    r_list = client.get("/jobs", headers=_auth("tok-bulk1"))
    assert len(r_list.json()) == 1


def test_submit_idempotency_scoped_per_user(client: TestClient) -> None:
    """User B replaying user A's key must NOT receive user A's job."""
    payload = _image_vo_payload()
    payload["idempotency_key"] = "sub-shared-key"
    r_a = client.post("/jobs", json=payload, headers=_auth("tok-bulk1"))
    r_b = client.post("/jobs", json=payload, headers=_auth("tok-bulk2"))
    assert r_a.status_code == 200
    assert r_b.status_code == 200
    # Different jobs — the scoping prevented a cross-user idempotency hit.
    assert r_a.json()["job_id"] != r_b.json()["job_id"]


def test_submit_idempotency_malformed_key_returns_400(client: TestClient) -> None:
    payload = _image_vo_payload()
    payload["idempotency_key"] = "has spaces and weird $%^ chars"
    r = client.post("/jobs", json=payload, headers=_auth("tok-bulk1"))
    assert r.status_code == 400


def test_submit_idempotency_oversized_key_returns_400(client: TestClient) -> None:
    payload = _image_vo_payload()
    payload["idempotency_key"] = "x" * 65    # one over the 64-char cap
    r = client.post("/jobs", json=payload, headers=_auth("tok-bulk1"))
    assert r.status_code == 400


def test_submit_without_idempotency_key_still_works(client: TestClient) -> None:
    """Backward compat: old Apps Script clients that don't send the key must
    keep working exactly as before."""
    payload = _image_vo_payload()
    # No idempotency_key field at all.
    r = client.post("/jobs", json=payload, headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    assert r.json()["job_id"].startswith("job-")


def test_submit_different_keys_create_separate_jobs(client: TestClient) -> None:
    """Different keys = different submits — even from the same user."""
    p1 = _image_vo_payload()
    p1["idempotency_key"] = "sub-aaa"
    p2 = _image_vo_payload()
    # Different row_num so the (sheet, worksheet, row_num) dedup doesn't merge.
    p2["rows_image_vo"][0]["row_num"] = 3
    p2["idempotency_key"] = "sub-bbb"
    r1 = client.post("/jobs", json=p1, headers=_auth("tok-bulk1"))
    r2 = client.post("/jobs", json=p2, headers=_auth("tok-bulk1"))
    assert r1.json()["job_id"] != r2.json()["job_id"]


# ── QueueBusy → 503 mapping (rule 18) ───────────────────────────────────────
# Plan: _plans/2026-06-04-submit-500-defensive-fix.md §"Change 3".
# SQLite OperationalError under lock contention must surface as 503 with
# Retry-After, so the Apps Script retries instead of showing a 500 toast.


def _patch_queue_to_raise_queuebusy(app: FastAPI, *, on_method: str) -> None:
    """Replace ``app.state.queue.<on_method>`` with a coroutine that raises
    ``QueueBusy`` — simulates SQLite contention without touching the DB."""
    from bulkvid.orchestrator.queue import QueueBusy

    async def _raises(*_args, **_kwargs):    # noqa: ANN002, ANN003
        raise QueueBusy("database is locked (simulated)")

    setattr(app.state.queue, on_method, _raises)


def test_submit_503_on_queue_busy(app: FastAPI, client: TestClient) -> None:
    _patch_queue_to_raise_queuebusy(app, on_method="enqueue")
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "5"
    # Body is generic; OperationalError details stay server-side only.
    assert "busy" in r.json()["detail"].lower()


def test_kill_503_on_queue_busy(app: FastAPI, client: TestClient) -> None:
    # First submit so there's a job to kill, BEFORE patching enqueue.
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    job_id = r.json()["job_id"]
    _patch_queue_to_raise_queuebusy(app, on_method="kill_job")
    r = client.post(f"/jobs/{job_id}/kill", headers=_auth("tok-bulk1"))
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "5"


def test_kill_all_503_on_queue_busy(app: FastAPI, client: TestClient) -> None:
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    _patch_queue_to_raise_queuebusy(app, on_method="kill_all_jobs")
    r = client.post("/jobs/kill-all", headers=_auth("tok-bulk1"))
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "5"


# ── Kill timeout -> 504 (Plan §B) ───────────────────────────────────────────
# Without the route-level ``asyncio.wait_for`` bound, a hung libsql roundtrip
# would pin the kill POST until the Apps Script's 30 s UrlFetch cap fired,
# and the operator saw "Could not kill" with no diagnostic. The 504 carries
# a clear "worker may be hung; restart the backend" message instead.
# Plan: ``_plans/2026-06-14-stuck-processing-rows.md`` §B.


def _patch_queue_method_to_hang(app: FastAPI, *, on_method: str) -> None:
    """Replace a queue async method with a coroutine that never returns —
    simulates a stalled libsql HTTP roundtrip. The route's ``wait_for``
    is the only thing that should unblock it."""
    import asyncio

    async def _hangs(*_args, **_kwargs):    # noqa: ANN002, ANN003
        await asyncio.Event().wait()

    setattr(app.state.queue, on_method, _hangs)


def test_kill_job_returns_504_on_timeout(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Shrink the bound so the test settles in <1 s.
    monkeypatch.setattr(jobs_routes, "_KILL_CALL_TIMEOUT_SECONDS", 0.1)
    # Submit so there's a job to kill, BEFORE patching kill_job.
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    job_id = r.json()["job_id"]
    _patch_queue_method_to_hang(app, on_method="kill_job")
    r = client.post(f"/jobs/{job_id}/kill", headers=_auth("tok-bulk1"))
    assert r.status_code == 504
    body = r.json()
    assert "kill timed out" in body["detail"].lower()
    assert "restart" in body["detail"].lower()


def test_kill_all_jobs_returns_504_on_timeout(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jobs_routes, "_KILL_CALL_TIMEOUT_SECONDS", 0.1)
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    _patch_queue_method_to_hang(app, on_method="kill_all_jobs")
    r = client.post("/jobs/kill-all", headers=_auth("tok-bulk1"))
    assert r.status_code == 504
    assert "kill timed out" in r.json()["detail"].lower()


def test_kill_job_returns_rows_aborted_field(client: TestClient) -> None:
    """Regression for plan §B: the kill response surfaces ``rows_aborted``
    so the Apps Script can show "Killed N rows" in the toast. Newly
    submitted job has one pending row; killing it must report 1."""
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    job_id = r.json()["job_id"]
    r = client.post(f"/jobs/{job_id}/kill", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert body["killed"] is True
    assert body["rows_aborted"] == 1


def test_kill_all_jobs_returns_rows_aborted_field(client: TestClient) -> None:
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    r = client.post("/jobs/kill-all", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert body["killed"] == 1
    assert body["rows_aborted"] == 1


# ── /jobs/poll timeouts (rule 18) ───────────────────────────────────────────
# Plan: ``_plans/2026-06-14-fast-fail-kill-and-poll-timeout.md`` §B.
# Without a per-call ``asyncio.wait_for`` bound, a stalled libsql roundtrip
# pinned the poll until the Apps Script's 30 s ``UrlFetch`` cap fired, and
# the sidebar sat on "Loading…" forever — which the operator mistook for the
# kill button being broken. ``list_jobs`` is mandatory and 504s on timeout
# so the sidebar's ``onFail`` shows "Reconnecting…"; the other four reads
# (rows, eta_medians, queue_depth, oldest_pending_age) are best-effort and
# drop their output silently rather than 504ing the whole poll.


def test_poll_returns_504_when_list_jobs_times_out(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``list_jobs`` is the one mandatory poll call. A timeout there has to
    surface as 504 with a diagnostic so the sidebar can flag the backend as
    unhealthy instead of sitting on a blank loader."""
    monkeypatch.setattr(jobs_routes, "_POLL_DB_CALL_TIMEOUT_SECONDS", 0.1)
    _patch_queue_method_to_hang(app, on_method="list_jobs")
    r = client.get("/jobs/poll", headers=_auth("tok-bulk1"))
    assert r.status_code == 504
    body = r.json()
    assert "poll timed out" in body["detail"].lower()
    assert "restart" in body["detail"].lower()


def test_poll_returns_partial_when_user_queue_depth_times_out(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled ``user_queue_depth`` must NOT 504 the whole poll. The
    sidebar still needs ``jobs`` to render; the queue-depth banner just
    hides itself (``queue_status`` is null) until the next cycle."""
    monkeypatch.setattr(jobs_routes, "_POLL_DB_CALL_TIMEOUT_SECONDS", 0.1)
    # Need at least one job present so the poll exercises the queue-depth
    # branch and the response contains something to assert against.
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    _patch_queue_method_to_hang(app, on_method="user_queue_depth")
    r = client.get("/jobs/poll", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert len(body["jobs"]) == 1
    assert body["queue_status"] is None


def test_poll_returns_partial_when_eta_medians_times_out(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled ``eta_medians`` falls back to an empty dict — same graceful
    degradation the existing try/except already provided, now tripped by
    the timeout bound instead of a raised exception."""
    monkeypatch.setattr(jobs_routes, "_POLL_DB_CALL_TIMEOUT_SECONDS", 0.1)
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    _patch_queue_method_to_hang(app, on_method="eta_medians")
    r = client.get("/jobs/poll", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert len(body["jobs"]) == 1
    assert body["eta_medians_by_tab"] == {}


def test_poll_returns_partial_when_list_rows_times_out(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-job ``list_rows`` is best-effort. A stalled call drops THAT
    job's row breakdown from ``rows_by_job`` but the rest of the poll
    completes normally — the sidebar renders the job card without rows."""
    monkeypatch.setattr(jobs_routes, "_POLL_DB_CALL_TIMEOUT_SECONDS", 0.1)
    # Need a RUNNING job for ``list_rows`` to be invoked at all.
    client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    claimed = await_(app.state.queue.claim_next_row())
    assert claimed is not None
    _patch_queue_method_to_hang(app, on_method="list_rows")
    r = client.get("/jobs/poll", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert len(body["jobs"]) == 1
    assert body["rows_by_job"] == {}


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


# ── chosen_template_id flows through the poll route ─────────────────────────


def test_row_to_out_forwards_chosen_template_id() -> None:
    """The queue's row dict carries ``chosen_template_id``; the route maps it
    into JobRowOut so the sidebar receives it on every poll cycle."""
    from bulkvid.routes.jobs import _row_to_out

    raw = {
        "row_num": 7,
        "status": "done",
        "started_at": "2026-06-07T12:00:00+00:00",
        "error": None,
        "video_urls": ["http://v1.mp4"],
        "chosen_template_id": "factual_hook",
    }
    out = _row_to_out("job-X", raw)
    assert out.chosen_template_id == "factual_hook"


def test_row_to_out_blank_chosen_template_id_becomes_none() -> None:
    """Empty string from the queue collapses to None on the wire — the sidebar
    treats null and missing identically and skips the caption."""
    from bulkvid.routes.jobs import _row_to_out

    raw = {
        "row_num": 7,
        "status": "done",
        "started_at": None,
        "error": None,
        "video_urls": [],
        "chosen_template_id": "",
    }
    out = _row_to_out("job-X", raw)
    assert out.chosen_template_id is None


def test_submit_queue_unavailable_returns_503(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the queue exhausts its reconnect+retry cycle and raises
    QueueUnavailable, the route returns 503 + Retry-After (NOT a bare 500), so
    the Apps Script retries the idempotent submit invisibly. Plan
    ``_plans/2026-06-17-submit-500s-turso-resilience.md``."""

    async def _boom(**kwargs: object) -> str:
        raise QueueUnavailable("turso unreachable after retries")

    monkeypatch.setattr(app.state.queue, "enqueue", _boom)
    r = client.post("/jobs", json=_image_vo_payload(), headers=_auth("tok-bulk1"))
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "5"
    # The fixed message never leaks the underlying error.
    assert "turso" not in r.text.lower()
