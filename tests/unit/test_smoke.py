"""Phase 0 smoke tests.

Confirms the package imports, settings load, the FastAPI app boots, and
``/health`` returns 200. If this file fails, nothing else can work.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bulkvid import __version__
from bulkvid.config import get_settings
from bulkvid.main import app


def test_version_present() -> None:
    assert isinstance(__version__, str)
    assert __version__.count(".") == 2


def test_settings_load_with_defaults() -> None:
    s = get_settings()
    assert s.BULKVID_PORT == 8788
    # ALLOWED_HD defaults to empty (multi-domain via env at deploy time).
    assert s.ALLOWED_HD == ""
    assert s.GCS_BUCKET_NAME == "aporia-unleash"
    assert s.AWS_BUCKET_NAME == "aporia-creative"


def test_health_endpoint_returns_ok() -> None:
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__


def test_app_lifespan_runs_without_crashing() -> None:
    """Catches NameErrors / import-time bugs in the lifespan handler."""
    # TestClient as a context manager actually triggers the lifespan startup
    # and shutdown. If main.py's lifespan handler references an undefined
    # name, this test fails (where the /health test above doesn't, because
    # the lifespan isn't triggered without the context manager form).
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200


def test_empty_allowlist_yields_empty_list() -> None:
    s = get_settings()
    # If .env hasn't been populated, the parsed list should be empty (not [""]).
    if s.BULK_TEAM_ALLOWLIST == "":
        assert s.bulk_team_emails == []
