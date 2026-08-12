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
import os
import signal
from contextlib import suppress
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
from bulkvid.orchestrator.db_watchdog import start_db_wedge_watchdog
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
from bulkvid.orchestrator.stuck_queue_watchdog import start_stuck_queue_watchdog

_log = get_logger("worker")


# ── Liveness heartbeat (Plan 2026-08-12) ────────────────────────────────────
#
# The worker lands a beat in the DB on this cadence. The beat is the ONLY signal
# that proves both halves of "this worker is alive": the event loop is still
# scheduling tasks (so the coroutine below runs at all) AND the worker's DB path
# still works (so the write lands). Every wedge to date broke at least one of
# those, which is why the independent stuck-queue watchdog restarts on a stale
# beat regardless of what the queue counts say — the shape-specific guards that
# gate on ``processing == 0`` all missed the 2026-08-12 mid-batch freeze.
#
# 30s gives the watchdog's 240s staleness threshold 8 chances to see a beat, so
# a transient flap (which ``_run_db`` heals in seconds) can never false-fire a
# restart. Env-tunable for a per-deploy tune without a code change.
_HEARTBEAT_INTERVAL_SECONDS = float(
    os.environ.get("BULKVID_WORKER_HEARTBEAT_INTERVAL_SECONDS") or 30.0
)


# ── Construction helpers ────────────────────────────────────────────────────


def build_pipeline_clients(settings: Settings) -> PipelineClients:
    """Construct the bundle. Required adapters fail fast on missing config;
    ZapCap is optional (set to ``None`` when no key configured)."""
    openai = openai_mod.build_client_from_settings(settings)
    # Per-sheet key routing: the router's ``default`` IS the client every
    # unmapped sheet uses, so wire ``kie`` to it (no pool built twice) and pass
    # the router through for the runner to swap per row. Plan
    # ``_plans/2026-07-20-per-sheet-kie-key-routing.md``.
    kie_router = kie_mod.build_router_from_settings(settings)
    kie = kie_router.default
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
        kie_router=kie_router,
    )


def build_flush_callback(settings: Settings) -> FlushCallback:
    """Pick the right write-back implementation for the environment.

    Production: ``SheetsClient.batch_write_video_urls`` with credentials from
    either ``SHEETS_SERVICE_ACCOUNT_FILE`` (a JSON path) or the inline
    ``GOOGLE_*`` env vars — whichever is configured.

    Local dev without ANY Sheets credentials: a noop callback that logs only —
    the worker still drains the queue, results are still recorded in SQLite.
    """
    from bulkvid.adapters import sheets as sheets_mod

    try:
        sheets_client = sheets_mod.build_client_from_settings(settings)
    except ValueError:
        _log.warning(
            "sheets_credentials_missing",
            note="worker will drain queue but skip sheet write-back",
        )

        async def _noop(writes: list[PendingWrite]) -> None:
            _log.info("sheets_writeback_skipped", count=len(writes))

        return _noop

    _log.info(
        "sheets_writer_attached",
        mode=(
            "file" if settings.SHEETS_SERVICE_ACCOUNT_FILE else "inline_env"
        ),
    )
    return sheets_client.batch_write_video_urls


async def heartbeat_loop(
    queue: JobQueue,
    runner: BatchRunner,
    *,
    interval_seconds: float = _HEARTBEAT_INTERVAL_SECONDS,
) -> None:
    """Land a liveness beat every ``interval_seconds``, forever.

    Beats FIRST, then sleeps, so the row is fresh within a moment of boot rather
    than one interval later — the stuck-queue watchdog's uptime gate depends on
    that not lagging.

    A failed beat is logged and swallowed on purpose. The beat is a *signal*,
    not a duty: if it cannot land, the watchdog seeing it go stale is precisely
    the outcome we want, whereas crashing the worker here would turn a
    diagnostic into an outage. Cancelled at shutdown by ``run``.
    """
    while True:
        try:
            await queue.write_heartbeat(in_flight=runner.in_flight_count)
        except Exception as e:    # noqa: BLE001 — a missed beat IS the signal
            _log.warning(
                "worker_heartbeat_write_failed",
                error=str(e)[:200],
                error_type=type(e).__name__,
            )
        await asyncio.sleep(interval_seconds)


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
        kie_mapped_sheets=len(settings.kie_key_map),
        sheet_writer_configured=bool(settings.SHEETS_SERVICE_ACCOUNT_FILE),
        kill_switch=bool(settings.BULKVID_KILL_SWITCH),
    )

    # Force a clean restart if the shared DB pool wedges on uncancellable libsql
    # calls (the mid-batch stall that only an HF restart used to fix). Covers the
    # gap the claim-failure watchdog can't — a wedge while rows are in flight.
    # Plan ``_plans/2026-07-07-db-wedge-permanent-fix.md`` §Phase 1.
    start_db_wedge_watchdog("worker")

    # Automate the manual "restart the Space": an independent daemon thread that
    # proves — via its OWN fresh connection — that real rows are queued while
    # nothing is in flight, and restarts a wedged worker when the stale-read
    # tripwire's in-process reconnect fails to drain it. Covers the outcome
    # (queue not draining) that the four cause-specific self-healers all miss.
    # Plan ``_plans/2026-08-06-worker-stuck-queue-restart-watchdog.md``.
    start_stuck_queue_watchdog(
        db_path=db_path,
        sync_url=settings.BULKVID_DB_URL,
        auth_token=settings.BULKVID_DB_AUTH_TOKEN,
        sync_interval_seconds=settings.BULKVID_DB_SYNC_INTERVAL_SECONDS,
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

    # Proof-of-life for the watchdog above. Started before the main gather so a
    # worker that wedges during its very first drain still has a fresh beat on
    # record to go stale from. Plan
    # ``_plans/2026-08-12-worker-liveness-net-heartbeat-forensics.md``.
    heartbeat = asyncio.create_task(heartbeat_loop(queue, runner))
    _log.info(
        "worker_heartbeat_start", interval_seconds=_HEARTBEAT_INTERVAL_SECONDS
    )

    try:
        await asyncio.gather(runner.run(), writer.run())
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
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
