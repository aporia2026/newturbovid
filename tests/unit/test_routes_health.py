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
from bulkvid.orchestrator.queue import JobQueue
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
        "tavily", "scrapingbee", "aws_s3", "gcs", "sheets",
    ):
        assert name in vendors

    # Concurrency block has the expected default values.
    assert body["concurrency"]["max_concurrent_rows"] >= 1

    # Empty recent_jobs is valid (we just booted the queue).
    assert body["queue"]["recent_jobs"] == []


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
