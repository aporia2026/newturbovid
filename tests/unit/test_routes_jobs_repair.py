"""Tests for the self-repair routes (Plan 2026-08-17).

``POST /jobs/repair``      — the operator's "Fix stuck jobs" button
``GET  /jobs/repair-log``  — what the automatic pass fixed while nobody watched

Reuses the fake-verifier app fixture shape from ``test_routes_jobs.py``. The
important properties here are not the happy path but the boundaries: a write
endpoint driven by a button must not cross between users, must not be swallowed
by the dynamic ``/{job_id}`` route, and must degrade to a retryable 503 rather
than a 500 when the DB flaps.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bulkvid.auth import AuthError, ForbiddenError, Identity
from bulkvid.orchestrator.queue import TAB_IMAGE_VO, JobQueue, QueueUnavailable
from bulkvid.routes import jobs as jobs_routes


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
    }

    async def verify(self, bearer_token: str) -> Identity:
        if bearer_token not in self.TOKENS:
            raise AuthError("unknown test token")
        identity = self.TOKENS[bearer_token]
        if identity is None:
            raise ForbiddenError("not on allowlist")
        return identity


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


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _payload() -> dict:
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


def _land_row_done(app: FastAPI, queue_id: int, row_num: int) -> None:
    """Apply ONLY the row half of a ``record_result``, reproducing the partial
    write that used to pin a finished job at ``running`` forever."""
    app.state.queue._conn.execute(
        "UPDATE row_queue SET status = 'done', "
        "finished_at = '2026-08-17T00:00:00+00:00', result = ? WHERE id = ?",
        (
            json.dumps({
                "row_num": row_num, "status": "SUCCESS",
                "video_urls": ["https://example.com/v.mp4"], "cost_usd": 0.0,
                "elapsed_seconds": 1.0, "error": None, "metadata": {},
            }),
            queue_id,
        ),
    )


async def _stuck_job(app: FastAPI, client: TestClient, token: str) -> str:
    """Create a job in the exact state the bug leaves behind: row finished, job
    still ``running``."""
    r = client.post("/jobs", json=_payload(), headers=_auth(token))
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    queued = await app.state.queue.claim_next_row()
    assert queued is not None
    _land_row_done(app, queued.id, 2)
    return job_id


# ── Auth + routing ──────────────────────────────────────────────────────────


def test_repair_requires_auth(client: TestClient) -> None:
    assert client.post("/jobs/repair").status_code == 401
    assert client.get("/jobs/repair-log").status_code == 401


def test_repair_log_is_not_swallowed_by_the_dynamic_job_route(
    client: TestClient,
) -> None:
    """``GET /jobs/repair-log`` must reach its own handler, not
    ``GET /jobs/{job_id}`` — the trap that made ``/jobs/avatars`` answer
    "job not found" until it was moved above the dynamic route."""
    r = client.get("/jobs/repair-log", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    assert r.json() == {"runs": []}


# ── Behaviour ───────────────────────────────────────────────────────────────


async def test_repair_finalizes_the_callers_stuck_job(
    app: FastAPI, client: TestClient,
) -> None:
    job_id = await _stuck_job(app, client, "tok-bulk1")

    r = client.post("/jobs/repair", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] >= 1
    assert "now marked done" in body["summary"]
    assert any("finalize_settled_jobs" in line for line in body["log"])

    job = client.get(f"/jobs/{job_id}", headers=_auth("tok-bulk1")).json()
    assert job["status"] == "completed"
    assert job["completed_rows"] == 1


async def test_repair_on_a_healthy_queue_says_so(
    app: FastAPI, client: TestClient,
) -> None:
    """The common case: the operator clicks it because something LOOKS wrong. The
    answer has to be a clear "nothing needed fixing", not an empty response that
    reads as a failure."""
    r = client.post("/jobs", json=_payload(), headers=_auth("tok-bulk1"))
    assert r.status_code == 200

    r = client.post("/jobs/repair", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    assert r.json()["changed"] == 0
    assert "Nothing needed fixing" in r.json()["summary"]


async def test_repair_cannot_touch_another_users_job(
    app: FastAPI, client: TestClient,
) -> None:
    """Blast radius: bulk2 clicking Fix must leave bulk1's identical strand
    exactly as it was."""
    job_id = await _stuck_job(app, client, "tok-bulk1")

    r = client.post("/jobs/repair", headers=_auth("tok-bulk2"))
    assert r.status_code == 200
    assert r.json()["changed"] == 0
    assert r.json()["scope"] == "bulk2@aporia.com"

    job = client.get(f"/jobs/{job_id}", headers=_auth("tok-bulk1")).json()
    assert job["status"] == "running"


async def test_admin_repair_covers_the_whole_fleet(
    app: FastAPI, client: TestClient,
) -> None:
    job_id = await _stuck_job(app, client, "tok-bulk1")

    r = client.post("/jobs/repair", headers=_auth("tok-admin"))
    assert r.status_code == 200
    assert r.json()["changed"] >= 1
    assert r.json()["scope"] == ""        # fleet-wide

    job = client.get(f"/jobs/{job_id}", headers=_auth("tok-admin")).json()
    assert job["status"] == "completed"


async def test_repair_log_shows_what_was_fixed(
    app: FastAPI, client: TestClient,
) -> None:
    await _stuck_job(app, client, "tok-bulk1")
    client.post("/jobs/repair", headers=_auth("tok-bulk1"))

    runs = client.get(
        "/jobs/repair-log", headers=_auth("tok-bulk1")
    ).json()["runs"]
    assert len(runs) == 1
    assert runs[0]["source"] == "manual"
    assert runs[0]["actor"] == "bulk1@aporia.com"
    assert runs[0]["changed"] >= 1
    assert "finalize_settled_jobs" in runs[0]["log"]


async def test_repair_log_does_not_leak_another_users_manual_run(
    app: FastAPI, client: TestClient,
) -> None:
    """A manual pass names the caller's own job ids in its log, so it stays
    visible to that caller and to admins only. (Automatic passes are counts-only
    and ARE shared — that is the vacation log.)"""
    await _stuck_job(app, client, "tok-bulk1")
    client.post("/jobs/repair", headers=_auth("tok-bulk1"))

    assert client.get(
        "/jobs/repair-log", headers=_auth("tok-bulk2")
    ).json()["runs"] == []
    assert len(
        client.get("/jobs/repair-log", headers=_auth("tok-admin")).json()["runs"]
    ) == 1


# ── Failure modes ───────────────────────────────────────────────────────────


def test_repair_maps_queue_unavailable_to_503(
    app: FastAPI, client: TestClient,
) -> None:
    """A flapping DB must be a retryable 503, not a 500 — same contract as
    submit and kill, so the Apps Script's own retry can handle it."""
    async def _unavailable(*_args, **_kwargs):
        raise QueueUnavailable("turso down (simulated)")

    app.state.queue.run_on_connection = _unavailable
    r = client.post("/jobs/repair", headers=_auth("tok-bulk1"))
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "5"


def test_repair_timeout_is_a_504_with_a_useful_message(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass that overruns must tell the operator the automatic one will pick it
    up, rather than leaving them to guess whether anything happened."""
    import asyncio

    monkeypatch.setattr(jobs_routes, "_REPAIR_CALL_TIMEOUT_SECONDS", 0.05)

    async def _hang(*_args, **_kwargs):
        await asyncio.sleep(5)

    app.state.queue.run_on_connection = _hang
    r = client.post("/jobs/repair", headers=_auth("tok-bulk1"))
    assert r.status_code == 504
    assert "automatic repair" in r.json()["detail"]


def test_repair_log_degrades_to_empty_instead_of_erroring(
    app: FastAPI, client: TestClient,
) -> None:
    """A missing log must never make the sidebar look broken."""
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("audit table on fire (simulated)")

    app.state.queue.run_on_connection = _boom
    r = client.get("/jobs/repair-log", headers=_auth("tok-bulk1"))
    assert r.status_code == 200
    assert r.json() == {"runs": []}
