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
from bulkvid.orchestrator.runtime_settings import registry_defaults
from bulkvid.orchestrator.settings_store import SettingsStore
from bulkvid.routes import admin as admin_routes
from bulkvid.routes import health as health_routes
from bulkvid.routes import jobs as jobs_routes

_log = get_logger("boot")


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    configure_logging()
    settings = get_settings()

    # Shared queue + settings store. The worker process opens its own
    # JobQueue + SettingsStore on the same DB files — SQLite WAL mode
    # makes that safe.
    data_dir = Path(settings.BULKVID_DATA_DIR)
    queue = JobQueue(data_dir / "jobs.db")
    settings_store = SettingsStore(
        data_dir / "settings.db", defaults=registry_defaults()
    )
    app.state.queue = queue
    app.state.settings_store = settings_store
    app.state.verifier = build_verifier_from_settings(settings)

    _log.info(
        "service_start",
        version=__version__,
        env=settings.BULKVID_ENV,
        port=settings.BULKVID_PORT,
        max_concurrent_rows=settings.BULKVID_MAX_CONCURRENT_ROWS,
        kie_keys_configured=len(settings.kie_key_list),
        kill_switch=bool(settings.BULKVID_KILL_SWITCH),
        bulk_team_count=len(settings.bulk_team_emails),
        admin_count=len(settings.admin_emails),
        data_dir=str(data_dir),
    )

    try:
        yield
    finally:
        queue.close()
        settings_store.close()
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


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — process is up. For deep status see ``/health/deep`` (admin-gated)."""
    return {"status": "ok", "version": __version__}
