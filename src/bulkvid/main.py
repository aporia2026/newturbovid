"""FastAPI entrypoint.

Plan: ``_plans/2026-06-02-aporia-bulk-video-tool.md``
- §5 Architecture
- §7 Security & Safety
- §8 Observability

Mounts:
  - GET /health         — liveness (Phase 0)
  - POST /jobs          — batch submit (Phase 4, auth-gated)
  - GET  /jobs          — list jobs
  - GET  /jobs/{id}     — single job status
  - POST /jobs/{id}/kill
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from bulkvid import __version__
from bulkvid.auth import build_verifier_from_settings
from bulkvid.config import get_settings
from bulkvid.logging import configure_logging, get_logger
from bulkvid.orchestrator.queue import JobQueue
from bulkvid.orchestrator.runtime_settings import (
    SETTING_SCRIPT_SYSTEM_PROMPT,
    SETTING_SIMPLE_SCRIPT_PROMPT,
    SETTING_SIMPLE_X4_SCRIPT_PROMPT,
    registry_defaults,
)
from bulkvid.orchestrator.settings_store import SettingsStore
from bulkvid.routes import admin as admin_routes
from bulkvid.routes import health as health_routes
from bulkvid.routes import jobs as jobs_routes
from bulkvid.tunnel import TunnelManager

_log = get_logger("boot")


def _build_state(app: FastAPI) -> None:
    """Create shared resources on ``app.state``. Idempotent.

    Called by the ASGI ``lifespan`` (uvicorn) AND by ``init_wsgi()`` (WSGI
    hosts like PythonAnywhere, where the ASGI lifespan never runs). The guard
    makes a double call (e.g. lifespan + wrapper) harmless.

    The worker process opens its own JobQueue + SettingsStore on the same DB
    files — SQLite WAL mode makes that safe.
    """
    if getattr(app.state, "queue", None) is not None:
        return
    settings = get_settings()
    data_dir = Path(settings.BULKVID_DATA_DIR)
    app.state.queue = JobQueue(data_dir / "jobs.db")
    app.state.settings_store = SettingsStore(
        data_dir / "settings.db", defaults=registry_defaults()
    )
    # One-shot migration: the legacy single ``script_system_prompt`` becomes
    # the per-tab ``simple_script_prompt`` + ``simple_x4_script_prompt`` so
    # any admin customization made before 2026-06-04 survives the split.
    # Idempotent — safe to call on every boot.
    app.state.settings_store.migrate_legacy_keys_sync(
        {
            SETTING_SCRIPT_SYSTEM_PROMPT: (
                SETTING_SIMPLE_SCRIPT_PROMPT,
                SETTING_SIMPLE_X4_SCRIPT_PROMPT,
            ),
        }
    )
    app.state.verifier = build_verifier_from_settings(settings)
    # Local-dev only: manages the cloudflared quick tunnel for the admin panel.
    # No-op in production (no cloudflared installed).
    app.state.tunnel = TunnelManager(settings.BULKVID_PORT, data_dir)
    _log.info(
        "service_start",
        version=__version__,
        env=settings.BULKVID_ENV,
        data_dir=str(data_dir),
        bulk_team_count=len(settings.bulk_team_emails),
        admin_count=len(settings.admin_emails),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    configure_logging()
    _build_state(app)
    try:
        yield
    finally:
        if getattr(app.state, "queue", None) is not None:
            app.state.queue.close()
        if getattr(app.state, "settings_store", None) is not None:
            app.state.settings_store.close()
        _log.info("service_stop")


app = FastAPI(
    title="TurboVid",
    description="Aporia bulk video creation tool",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(jobs_routes.router)
app.include_router(health_routes.router)
app.include_router(admin_routes.router)


def init_wsgi() -> FastAPI:
    """Entrypoint for WSGI hosts (PythonAnywhere) where the ASGI lifespan does
    not run. Configures logging, builds ``app.state``, and returns the app to
    be wrapped by ``a2wsgi.ASGIMiddleware``."""
    configure_logging()
    _build_state(app)
    return app


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — process is up. For deep status see ``/health/deep`` (admin-gated)."""
    return {"status": "ok", "version": __version__}
