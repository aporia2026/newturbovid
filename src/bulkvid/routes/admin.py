"""Admin panel — minimal read-only-plus-kill dashboard.

HTTP Basic auth via ``ADMIN_PANEL_USERNAME`` + ``ADMIN_PANEL_PASSWORD`` env
vars (separate from the bulk team's OAuth flow). Leave the env vars empty to
disable the panel on a given deploy.

Routes:
  - GET  /admin/             — dashboard with the 50 most recent jobs
  - GET  /admin/jobs/{id}    — single-job detail
  - POST /admin/jobs/{id}/kill — kill (HTMX form post)

Plan §9 (Settings / Admin Panel — read-only MVP), Phase 5.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from bulkvid.config import get_settings
from bulkvid.logging import get_logger
from bulkvid.orchestrator.queue import JobQueue
from bulkvid.orchestrator.runtime_settings import SETTINGS_REGISTRY, lookup
from bulkvid.orchestrator.settings_store import SettingsStore

_log = get_logger("admin")


_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "admin" / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


router = APIRouter(prefix="/admin", tags=["admin"])

_security = HTTPBasic()


def _check_admin(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    s = get_settings()
    if not s.ADMIN_PANEL_USERNAME or not s.ADMIN_PANEL_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="admin panel not configured on this deploy",
        )
    # Constant-time compare on both fields.
    name_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"), s.ADMIN_PANEL_USERNAME.encode("utf-8")
    )
    pass_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"), s.ADMIN_PANEL_PASSWORD.encode("utf-8")
    )
    if not (name_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="incorrect credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def _get_queue(request: Request) -> JobQueue:
    queue: JobQueue | None = getattr(request.app.state, "queue", None)
    if queue is None:
        raise HTTPException(500, "queue not configured")
    return queue


def _get_settings_store(request: Request) -> SettingsStore:
    store: SettingsStore | None = getattr(request.app.state, "settings_store", None)
    if store is None:
        raise HTTPException(500, "settings_store not configured")
    return store


# ── Routes ──────────────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    _user: str = Depends(_check_admin),
) -> HTMLResponse:
    queue = _get_queue(request)
    jobs = await queue.list_jobs(user_email=None, limit=50)
    return templates.TemplateResponse(
        request, "dashboard.html", {"jobs": jobs}
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(
    request: Request,
    job_id: str,
    _user: str = Depends(_check_admin),
) -> HTMLResponse:
    queue = _get_queue(request)
    job = await queue.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    rows = await queue.list_rows(job_id)
    return templates.TemplateResponse(
        request, "job_detail.html", {"job": job, "rows": rows}
    )


@router.post("/jobs/{job_id}/kill", response_class=HTMLResponse)
async def kill_job(
    request: Request,
    job_id: str,
    _user: str = Depends(_check_admin),
) -> HTMLResponse:
    queue = _get_queue(request)
    killed = await queue.kill_job(job_id)
    _log.info("admin_kill_job", job_id=job_id, killed=killed)
    job = await queue.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    # HTMX swaps just the status badge.
    return templates.TemplateResponse(
        request, "_status_badge.html", {"job": job}
    )


# ── Settings (editable runtime config) ───────────────────────────────────────


@router.get("/settings", response_class=HTMLResponse)
async def settings_list(
    request: Request,
    user: str = Depends(_check_admin),
) -> HTMLResponse:
    store = _get_settings_store(request)
    values = await store.get_all()
    items = [
        {
            "key": s.key,
            "label": s.label,
            "description": s.description,
            "multiline": s.multiline,
            "value": values.get(s.key, s.default),
            "is_default": values.get(s.key, s.default) == s.default,
        }
        for s in SETTINGS_REGISTRY
    ]
    return templates.TemplateResponse(
        request, "settings_list.html", {"items": items}
    )


@router.get("/settings/{key}", response_class=HTMLResponse)
async def settings_detail(
    request: Request,
    key: str,
    user: str = Depends(_check_admin),
) -> HTMLResponse:
    setting = lookup(key)
    if setting is None:
        raise HTTPException(404, "unknown setting key")
    store = _get_settings_store(request)
    current = await store.get(key, default=setting.default)
    audit = await store.audit(key=key, limit=20)
    return templates.TemplateResponse(
        request,
        "settings_detail.html",
        {
            "setting": setting,
            "current_value": current,
            "is_default": current == setting.default,
            "audit": audit,
        },
    )


@router.post("/settings/{key}")
async def settings_save(
    request: Request,
    key: str,
    value: str = Form(...),
    user: str = Depends(_check_admin),
) -> RedirectResponse:
    setting = lookup(key)
    if setting is None:
        raise HTTPException(404, "unknown setting key")
    store = _get_settings_store(request)
    old = await store.set(key, value, updated_by=user)
    _log.info(
        "admin_setting_changed",
        key=key,
        updated_by=user,
        old_chars=len(old) if old else 0,
        new_chars=len(value),
    )
    return RedirectResponse(
        url=f"/admin/settings/{key}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/settings/{key}/reset")
async def settings_reset(
    request: Request,
    key: str,
    user: str = Depends(_check_admin),
) -> RedirectResponse:
    setting = lookup(key)
    if setting is None:
        raise HTTPException(404, "unknown setting key")
    store = _get_settings_store(request)
    await store.set(key, setting.default, updated_by=user)
    _log.info("admin_setting_reset", key=key, updated_by=user)
    return RedirectResponse(
        url=f"/admin/settings/{key}", status_code=status.HTTP_303_SEE_OTHER
    )


# ── Tunnel (local-dev: regenerate the cloudflared public URL) ─────────────────


@router.get("/tunnel", response_class=HTMLResponse)
async def tunnel_page(
    request: Request, _user: str = Depends(_check_admin)
) -> HTMLResponse:
    mgr = getattr(request.app.state, "tunnel", None)
    available = bool(mgr and mgr.available())
    return templates.TemplateResponse(
        request,
        "tunnel.html",
        {"available": available, "url": mgr.current_url() if mgr else None, "error": None},
    )


@router.post("/tunnel/regenerate", response_class=HTMLResponse)
async def tunnel_regenerate(
    request: Request, _user: str = Depends(_check_admin)
) -> HTMLResponse:
    mgr = getattr(request.app.state, "tunnel", None)
    available = bool(mgr and mgr.available())
    url = mgr.current_url() if mgr else None
    error = None
    if not available:
        error = "cloudflared is not available on this host — nothing to regenerate."
    else:
        try:
            url = await mgr.regenerate()
        except Exception as e:    # surface the failure in the page
            error = str(e)
    _log.info("admin_tunnel_regenerate", ok=bool(error is None), error=error)
    return templates.TemplateResponse(
        request, "tunnel.html", {"available": available, "url": url, "error": error}
    )


# ── Per-row logs ──────────────────────────────────────────────────────────────


def _format_log_line(raw: str) -> str:
    """Turn one stored JSON log line into a compact readable string."""
    try:
        d = json.loads(raw)
    except Exception:
        return raw
    ts = str(d.get("timestamp", ""))[11:19]
    lvl = str(d.get("level", "")).upper()[:4]
    event = d.get("event", "")
    skip = {"timestamp", "level", "event", "ns", "batch_id", "row_num", "user_email"}
    kv = " ".join(f"{k}={v}" for k, v in d.items() if k not in skip)
    return f"{ts} {lvl:<4} {event} {kv}".rstrip()


@router.get("/jobs/{job_id}/logs", response_class=HTMLResponse)
async def job_logs(
    request: Request,
    job_id: str,
    row: int | None = None,
    tail: int = 300,
    _user: str = Depends(_check_admin),
) -> HTMLResponse:
    safe = job_id.replace("/", "_").replace("\\", "_").replace("..", "_")
    path = Path(get_settings().BULKVID_DATA_DIR) / "logs" / f"{safe}.log"
    lines: list[str] = []
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if row is not None:
                try:
                    if int(json.loads(raw).get("row_num", -1)) != row:
                        continue
                except Exception:
                    continue
            lines.append(_format_log_line(raw))
    tail = max(1, min(tail, 2000))
    return templates.TemplateResponse(
        request,
        "logs.html",
        {"job_id": job_id, "row": row, "lines": lines[-tail:], "exists": path.exists()},
    )
