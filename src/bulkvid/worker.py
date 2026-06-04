"""Worker entrypoint — drains the SQLite job queue and processes rows.

Long-running async process. Lives as:
  - PythonAnywhere: the single always-on task (``python -m bulkvid.worker``)
  - Hetzner / Docker: the ``bulkvid-worker`` service
  - Local dev:    second terminal, same command

Shares all modules with the FastAPI app (config, adapters, pipeline,
orchestrator). The only difference between deploys is **who launches this
script** and **what concurrency is configured**.

Plan: ``_plans/2026-06-02-aporia-bulk-video-tool.md`` §5 ("Process split").
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any

from bulkvid.adapters import article_fetch as article_mod
from bulkvid.adapters import atlascloud as atlas_mod
from bulkvid.adapters import gemini_tts as tts_mod
from bulkvid.adapters import kie as kie_mod
from bulkvid.adapters import openai_client as openai_mod
from bulkvid.adapters import rendi as rendi_mod
from bulkvid.adapters import storage as storage_mod
from bulkvid.adapters import zapcap as zapcap_mod
from bulkvid.adapters.sheets import SheetsClient
from bulkvid.config import Settings, get_settings
from bulkvid.logging import configure_logging, get_logger
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.queue import JobQueue
from bulkvid.orchestrator.runner import BatchRunner
from bulkvid.orchestrator.runtime_settings import (
    SETTING_SCRIPT_SYSTEM_PROMPT,
    SETTING_SIMPLE_SCRIPT_PROMPT,
    SETTING_SIMPLE_X4_SCRIPT_PROMPT,
    registry_defaults,
)
from bulkvid.orchestrator.settings_store import SettingsStore
from bulkvid.orchestrator.sheet_writer import (
    CoalescedSheetWriter,
    FlushCallback,
    PendingWrite,
)

_log = get_logger("worker")


# ── Construction helpers ────────────────────────────────────────────────────


def build_pipeline_clients(settings: Settings) -> PipelineClients:
    """Construct the bundle. Required adapters fail fast on missing config;
    ZapCap is optional (set to ``None`` when no key configured)."""
    openai = openai_mod.build_client_from_settings(settings)
    kie = kie_mod.build_client_from_settings(settings)
    tts = tts_mod.build_client_from_settings(settings)
    rendi = rendi_mod.build_client_from_settings(settings)
    storage = storage_mod.build_client_from_settings(settings)
    article = article_mod.build_fetcher_from_settings(settings)
    zapcap = (
        zapcap_mod.build_client_from_settings(settings)
        if settings.ZAPCAP_API_KEY
        else None
    )
    # AtlasCloud is an optional fallback for kie.ai. Returns None when no
    # key is configured.
    atlas = atlas_mod.build_client_from_settings(settings)

    return PipelineClients(
        openai=openai,
        kie=kie,
        tts=tts,
        rendi=rendi,
        storage=storage,
        article=article,
        zapcap=zapcap,
        atlas=atlas,
    )


def build_flush_callback(settings: Settings) -> FlushCallback:
    """Pick the right write-back implementation for the environment.

    Production: ``SheetsClient.batch_write_video_urls``.
    Local dev without Sheets credentials: a noop callback that logs only —
    the worker still drains the queue, results are still recorded in SQLite.
    """
    if settings.SHEETS_SERVICE_ACCOUNT_FILE:
        sheets = SheetsClient(credentials_file=settings.SHEETS_SERVICE_ACCOUNT_FILE)
        _log.info("sheets_writer_attached", file=settings.SHEETS_SERVICE_ACCOUNT_FILE)
        return sheets.batch_write_video_urls

    _log.warning(
        "sheets_credentials_missing",
        note="worker will drain queue but skip sheet write-back",
    )

    async def _noop(writes: list[PendingWrite]) -> None:
        _log.info("sheets_writeback_skipped", count=len(writes))

    return _noop


# ── Main loop ───────────────────────────────────────────────────────────────


async def run() -> None:
    configure_logging()
    settings = get_settings()

    data_dir = Path(settings.BULKVID_DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "jobs.db"

    # Settings store falls back to the jobs DB token/URL when its own pair
    # is empty (single-DB Turso deploy). Matches the rule applied in
    # main.py::_build_state so web app and worker can't disagree.
    settings_db_url = settings.BULKVID_SETTINGS_DB_URL or settings.BULKVID_DB_URL
    settings_db_token = (
        settings.BULKVID_SETTINGS_DB_AUTH_TOKEN or settings.BULKVID_DB_AUTH_TOKEN
    )
    queue = JobQueue(
        db_path,
        sync_url=settings.BULKVID_DB_URL,
        auth_token=settings.BULKVID_DB_AUTH_TOKEN,
        sync_interval_seconds=settings.BULKVID_DB_SYNC_INTERVAL_SECONDS,
    )
    settings_store = SettingsStore(
        data_dir / "settings.db",
        defaults=registry_defaults(),
        sync_url=settings_db_url,
        auth_token=settings_db_token,
        sync_interval_seconds=settings.BULKVID_DB_SYNC_INTERVAL_SECONDS,
    )
    # Migrate the legacy single-prompt key to the per-tab keys. Web app does
    # the same on its boot; both running it is safe — the inner check skips
    # already-populated keys.
    settings_store.migrate_legacy_keys_sync(
        {
            SETTING_SCRIPT_SYSTEM_PROMPT: (
                SETTING_SIMPLE_SCRIPT_PROMPT,
                SETTING_SIMPLE_X4_SCRIPT_PROMPT,
            ),
        }
    )
    clients = build_pipeline_clients(settings)
    clients.settings_store = settings_store

    writer = CoalescedSheetWriter(
        flush_callback=build_flush_callback(settings),
        flush_interval_seconds=settings.BULKVID_SHEET_WRITE_INTERVAL_SECONDS,
    )
    runner = BatchRunner(
        queue,
        clients,
        max_concurrent=settings.BULKVID_MAX_CONCURRENT_ROWS,
        write_back=writer.submit,
    )

    _log.info(
        "worker_start",
        env=settings.BULKVID_ENV,
        db_path=str(db_path),
        max_concurrent_rows=settings.BULKVID_MAX_CONCURRENT_ROWS,
        kie_keys_configured=len(settings.kie_key_list),
        sheet_writer_configured=bool(settings.SHEETS_SERVICE_ACCOUNT_FILE),
        kill_switch=bool(settings.BULKVID_KILL_SWITCH),
    )

    # ── Wire shutdown signals ───────────────────────────────────────────
    def _handle_signal(*_: Any) -> None:
        _log.info("worker_signal_received")
        runner.request_shutdown()
        writer.request_shutdown()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows: signal handlers are limited; the dev experience is to
            # Ctrl-C the process and let the KeyboardInterrupt propagate.
            pass

    try:
        await asyncio.gather(runner.run(), writer.run())
    finally:
        queue.close()
        settings_store.close()
        _log.info("worker_stop")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        # Clean exit on Ctrl-C without a noisy stack trace.
        pass


if __name__ == "__main__":
    main()
