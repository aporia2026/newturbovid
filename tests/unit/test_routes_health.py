"""Tests for /health/deep.

Covers:
  - Unauthorized -> 401
  - Bulk user (non-admin) -> 403
  - Admin -> 200 with vendor + concurrency + cost_guards + queue summary
  - API keys are masked (suffix only)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bulkvid.auth import AuthError, ForbiddenError, Identity
from bulkvid.orchestrator.queue import JobQueue, record_wedge_forensics
from bulkvid.routes import health as health_routes
from bulkvid.routes import jobs as jobs_routes


class _FakeVerifier:
    TOKENS = {
        "tok-bulk": Identity(
            email="bulk1@aporia.com", hd="aporia.com", name="B", is_admin=False
        ),
        "tok-admin": Identity(
            email="yoav@aporia.com", hd="aporia.com", name="Y", is_admin=True
        ),
    }

    async def verify(self, bearer_token: str) -> Identity:
        if bearer_token not in self.TOKENS:
            raise AuthError("bad token")
        return self.TOKENS[bearer_token]


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    a = FastAPI()
    a.include_router(jobs_routes.router)
    a.include_router(health_routes.router)
    a.state.queue = JobQueue(tmp_path / "jobs.db")
    a.state.verifier = _FakeVerifier()
    yield a
    a.state.queue.close()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _auth(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


def test_deep_health_no_auth_returns_401(client: TestClient) -> None:
    r = client.get("/health/deep")
    assert r.status_code == 401


def test_deep_health_bulk_user_returns_403(client: TestClient) -> None:
    r = client.get("/health/deep", headers=_auth("tok-bulk"))
    assert r.status_code == 403


def test_deep_health_admin_returns_full_status(client: TestClient) -> None:
    r = client.get("/health/deep", headers=_auth("tok-admin"))
    assert r.status_code == 200
    body = r.json()

    assert body["service"] == "bulkvid"
    assert "vendors" in body
    assert "concurrency" in body
    assert "cost_guards" in body
    assert "allowlists" in body
    assert "queue" in body

    # Vendor block has expected keys.
    vendors = body["vendors"]
    for name in (
        "openai", "kie_ai", "vertex_ai", "rendi", "zapcap",
        "scrapingbee", "aws_s3", "gcs", "sheets",
    ):
        assert name in vendors

    # Concurrency block has the expected default values.
    assert body["concurrency"]["max_concurrent_rows"] >= 1

    # Empty recent_jobs is valid (we just booted the queue).
    assert body["queue"]["recent_jobs"] == []


def test_deep_health_recognizes_inline_google_credentials(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GCS + Vertex are configured via inline env vars (no file path).

    Regression: the deep check used to look only at the file-path credential
    vars and reported these as unconfigured even when the inline GOOGLE_* /
    VERTEX_AI_* vars (the path actually used in deploy) were set.
    """
    from bulkvid.config import Settings

    pem = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n"
    s = Settings(
        _env_file=None,    # hermetic: ignore any local .env
        GCS_BUCKET_NAME="aporia-unleash",
        GOOGLE_PRIVATE_KEY=pem,
        GOOGLE_CLIENT_EMAIL="storage@proj.iam.gserviceaccount.com",
        VERTEX_AI_PRIVATE_KEY=pem,
        VERTEX_AI_CLIENT_EMAIL="tts@amit-tts.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(health_routes, "get_settings", lambda: s)

    r = client.get("/health/deep", headers=_auth("tok-admin"))
    assert r.status_code == 200
    vendors = r.json()["vendors"]
    assert vendors["gcs"]["configured"] is True
    assert vendors["vertex_ai"]["credentials_configured"] is True


def test_deep_health_does_not_leak_api_keys(client: TestClient) -> None:
    r = client.get("/health/deep", headers=_auth("tok-admin"))
    assert r.status_code == 200
    body_str = r.content.decode("utf-8")
    # The .env.example sentinel values should never appear in full anywhere.
    # Test fixture's settings have empty keys, but the rule should still hold.
    # We also confirm no field literally exposes a "key" field with a long string.
    vendors = r.json()["vendors"]
    openai = vendors["openai"]
    # Only "configured" + "suffix" are present, never a raw key.
    assert set(openai.keys()) == {"configured", "suffix"}


# ── Worker liveness + wedge history (Plan 2026-08-12) ───────────────────────


def test_deep_health_reports_no_heartbeat_before_worker_beats(
    client: TestClient,
) -> None:
    """A freshly booted DB has no beat. That is reported as ``None`` rather than
    a fake age, so nobody mistakes "never beaten" for "beaten just now"."""
    r = client.get("/health/deep", headers=_auth("tok-admin"))
    assert r.status_code == 200
    assert r.json()["worker"]["heartbeat"] is None


async def test_deep_health_reports_heartbeat_age(
    app: FastAPI, client: TestClient
) -> None:
    await app.state.queue.write_heartbeat(in_flight=3)
    r = client.get("/health/deep", headers=_auth("tok-admin"))
    assert r.status_code == 200
    beat = r.json()["worker"]["heartbeat"]
    assert beat["in_flight"] == 3
    assert 0 <= beat["age_seconds"] < 60
    assert beat["pid"] > 0


async def test_deep_health_lists_recent_wedges_with_truncated_stacks(
    app: FastAPI, client: TestClient
) -> None:
    """The page previews the autopsy; the full dump stays in the DB so a huge
    stack can never bloat the health response."""
    record_wedge_forensics(
        app.state.queue._conn, process="worker",
        reason="worker_heartbeat_stale", pending=109, processing=6,
        heartbeat_age_s=312.5, stacks="X" * 5000,
    )
    r = client.get("/health/deep", headers=_auth("tok-admin"))
    assert r.status_code == 200
    wedges = r.json()["worker"]["recent_wedges"]
    assert len(wedges) == 1
    assert wedges[0]["reason"] == "worker_heartbeat_stale"
    assert (wedges[0]["pending"], wedges[0]["processing"]) == (109, 6)
    assert len(wedges[0]["stacks_preview"]) == 2000


def test_deep_health_degrades_when_heartbeat_read_fails(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB blip must degrade the page, not 500 it — this endpoint is what an
    operator opens WHILE things are broken."""
    async def _boom(*_a, **_k):
        raise RuntimeError("turso unreachable")

    monkeypatch.setattr(app.state.queue, "read_heartbeat", _boom)
    monkeypatch.setattr(app.state.queue, "list_wedge_forensics", _boom)
    r = client.get("/health/deep", headers=_auth("tok-admin"))
    assert r.status_code == 200
    worker = r.json()["worker"]
    assert "turso unreachable" in worker["heartbeat_error"]
    assert "turso unreachable" in worker["recent_wedges_error"]
