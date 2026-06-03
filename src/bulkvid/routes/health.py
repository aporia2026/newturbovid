"""Deep health / configuration inspection route.

``GET /health/deep`` — admin-only summary of which services are configured,
how many keys, queue stats, kill-switch state. Mirrors the diagnostic
shape from ``refs/creative_builder_dev/cb_health_check.py`` adapted to our
adapter set.

This is intentionally a *configuration* health endpoint, not a vendor-ping
endpoint. Pinging every vendor on every health check would burn API quota.
Phase 5 admin panel adds a "Run live health check" button that does the
actual pings on demand.

Plan §8 (Observability), §9 (admin panel kill-switch indicator).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from bulkvid.adapters.google_credentials import (
    build_vertex_credentials_info,
    have_credentials_configured,
)
from bulkvid.auth import Identity
from bulkvid.config import get_settings
from bulkvid.logging import get_logger
from bulkvid.orchestrator.queue import JobQueue
from bulkvid.routes.jobs import get_identity, get_queue

_log = get_logger("route.health")


router = APIRouter(prefix="/health", tags=["health"])


@router.get("/deep")
async def deep_health(
    identity: Identity = Depends(get_identity),
    queue: JobQueue = Depends(get_queue),
) -> dict[str, Any]:
    """Admin-only deep status: configuration + queue summary."""
    if not identity.is_admin:
        raise HTTPException(403, "admin only")

    settings = get_settings()

    # Mask values; only show presence + key suffix (last 4 chars).
    def _present(value: str) -> dict[str, Any]:
        v = (value or "").strip()
        return {
            "configured": bool(v),
            "suffix": ("…" + v[-4:]) if v else "",
        }

    recent = await queue.list_jobs(user_email=None, limit=10)

    return {
        "service": "bulkvid",
        "env": settings.BULKVID_ENV,
        "kill_switch": bool(settings.BULKVID_KILL_SWITCH),
        "vendors": {
            "openai": _present(settings.OPENAI_API_KEY),
            "kie_ai": {
                "configured": len(settings.kie_key_list) > 0,
                "key_count": len(settings.kie_key_list),
                "suffixes": [k[-4:] for k in settings.kie_key_list],
            },
            "vertex_ai": {
                # Recognise BOTH credential modes the TTS client actually uses:
                # a file path OR inline VERTEX_AI_* / GOOGLE_* env vars.
                "credentials_configured": build_vertex_credentials_info(settings)
                is not None,
                "project": settings.VERTEX_AI_PROJECT_ID,
                "location": settings.VERTEX_AI_LOCATION,
            },
            "rendi": _present(settings.RENDI_API_KEY),
            "zapcap": _present(settings.ZAPCAP_API_KEY),
            "tavily": _present(settings.TAVILY_API_KEY),
            "scrapingbee": _present(settings.SCRAPINGBEE_API_KEY),
            "aws_s3": {
                "configured": bool(
                    settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY
                ),
                "bucket": settings.AWS_BUCKET_NAME,
                "region": settings.AWS_REGION,
            },
            "gcs": {
                # Mirror build_client_from_settings: a bucket plus credentials
                # via a GCS file path, the shared GOOGLE_* inline vars, or a
                # GOOGLE_APPLICATION_CREDENTIALS file path.
                "configured": bool(
                    settings.GCS_BUCKET_NAME
                    and (
                        settings.GCS_CREDENTIALS_FILE
                        or have_credentials_configured(settings)
                    )
                ),
                "bucket": settings.GCS_BUCKET_NAME,
            },
            "sheets": _present(settings.SHEETS_SERVICE_ACCOUNT_FILE),
        },
        "concurrency": {
            "max_concurrent_rows": settings.BULKVID_MAX_CONCURRENT_ROWS,
            "max_rows_per_batch": settings.BULKVID_MAX_ROWS_PER_BATCH,
            "sheet_write_interval_seconds": settings.BULKVID_SHEET_WRITE_INTERVAL_SECONDS,
        },
        "cost_guards": {
            "per_batch_usd_cap": settings.BULKVID_COST_PER_BATCH_USD_CAP,
            "per_day_usd_cap": settings.BULKVID_COST_PER_DAY_USD_CAP,
            "per_month_usd_cap": settings.BULKVID_COST_PER_MONTH_USD_CAP,
        },
        "allowlists": {
            "allowed_hd": settings.ALLOWED_HD,
            "bulk_team_count": len(settings.bulk_team_emails),
            "admin_count": len(settings.admin_emails),
        },
        "queue": {
            "recent_jobs": [
                {
                    "job_id": j.job_id,
                    "user_email": j.user_email,
                    "status": j.status,
                    "row_count": j.row_count,
                    "completed_rows": j.completed_rows,
                    "failed_rows": j.failed_rows,
                    "cost_usd": j.cost_usd,
                }
                for j in recent
            ],
        },
    }
