"""Global unhandled-exception handler: any unmapped exception → clean 503.

The submit/poll paths have two network dependencies that can raise (Turso and
Google JWKS verification). The catch-all handler in ``bulkvid.main`` ensures
that even a cause we did not anticipate degrades to an invisible client retry
instead of the bare ``HTTP 500 Internal Server Error`` popup the operator saw.

Plan: ``_plans/2026-06-17-submit-500s-turso-resilience.md`` §Change 4.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import bulkvid.main as main_mod


def _app_with_handler() -> FastAPI:
    app = FastAPI()
    # Register the REAL handler under test, plus probe routes.
    app.add_exception_handler(Exception, main_mod._unhandled_exception_handler)

    @app.get("/boom")
    def boom() -> dict[str, str]:
        raise RuntimeError("kaboom-secret-detail")

    @app.get("/teapot")
    def teapot() -> dict[str, str]:
        raise HTTPException(status_code=418, detail="i am a teapot")

    return app


def test_unhandled_exception_returns_503_not_500() -> None:
    client = TestClient(_app_with_handler(), raise_server_exceptions=False)
    r = client.get("/boom")
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "5"
    assert r.json() == {"detail": "backend temporarily unavailable, please retry"}


def test_unhandled_exception_does_not_leak_detail() -> None:
    client = TestClient(_app_with_handler(), raise_server_exceptions=False)
    r = client.get("/boom")
    # The exception message must never reach the client.
    assert "kaboom-secret-detail" not in r.text


def test_http_exception_still_passes_through() -> None:
    """The catch-all must NOT swallow explicit HTTPExceptions — 4xx codes
    (and 422 validation errors) keep their own behaviour."""
    client = TestClient(_app_with_handler(), raise_server_exceptions=False)
    r = client.get("/teapot")
    assert r.status_code == 418
    assert r.json() == {"detail": "i am a teapot"}
