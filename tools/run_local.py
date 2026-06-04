"""Local bulk-video runner — bypass PythonAnywhere for one specific user.

One-off CLI for the bulk-team user who wants to process Sheet rows on his own
machine when PA is flaky. Reads rows directly from a Google Sheet via the
service-account adapter, dispatches each row to the SAME per-row processor PA
uses, writes results back as they finish.

Zero pipeline logic lives here. Every step in ``src/bulkvid/`` — adapters,
pipeline modules, row processors, settings store — is consumed as-is. The
maintenance contract is "git pull + rerun"; see
``_plans/2026-06-04-local-runner-script.md`` for the design.

Usage:
    python tools/run_local.py \\
        --sheet-id <google sheet id> \\
        --worksheet "image_vo" \\
        --layout image_vo \\
        --rows 5,7,9-12

    python tools/run_local.py --help

``--worksheet`` is the literal tab name at the bottom of the Sheet (whatever
the bulk team labelled it). ``--layout`` is which of the four row layouts to
use (the column structure), independent of the tab name.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from bulkvid.adapters.google_credentials import build_credentials_info
from bulkvid.adapters.sheets import (
    SheetsClient,
    build_client_from_settings as build_sheets_client,
)
from bulkvid.config import Settings, get_settings
from bulkvid.logging import configure_logging, get_logger, set_context
from bulkvid.models.row import (
    STATUS_INTERNAL_ERROR,
    STATUS_SUCCESS,
    ImageVORow,
    RowResult,
    SimpleRow,
)
from bulkvid.orchestrator.clients import PipelineClients
from bulkvid.orchestrator.queue import (
    TAB_CARTOON,
    TAB_FOUR_IMAGES,
    TAB_IMAGE_VO,
    TAB_SIMPLE,
)
from bulkvid.orchestrator.row_processor_4images import process_4images_vo2_row
from bulkvid.orchestrator.row_processor_cartoon import process_cartoon_row
from bulkvid.orchestrator.row_processor_image_vo import process_image_vo_row
from bulkvid.orchestrator.row_processor_simple import process_simple_row
from bulkvid.orchestrator.runtime_settings import (
    SETTING_SCRIPT_SYSTEM_PROMPT,
    SETTING_SIMPLE_SCRIPT_PROMPT,
    SETTING_SIMPLE_X4_SCRIPT_PROMPT,
    registry_defaults,
)
from bulkvid.orchestrator.settings_store import SettingsStore
from bulkvid.orchestrator.sheet_writer import PendingWrite
from bulkvid.worker import build_pipeline_clients

_log = get_logger("run_local")


# ── Constants ───────────────────────────────────────────────────────────────


ALLOWED_TABS = (TAB_IMAGE_VO, TAB_FOUR_IMAGES, TAB_SIMPLE, TAB_CARTOON)


# ── Layout auto-detection ───────────────────────────────────────────────────
#
# Mirrors ``_detectTabType`` in ``apps_script/Code.gs`` so the local runner
# picks the same layout the PA Apps Script menu would pick for the same tab.


def infer_layout_from_name(worksheet_name: str) -> str | None:
    """Detect the row layout from the worksheet's tab name.

    Returns one of the ``TAB_*`` constants, or ``None`` if the name carries no
    layout signal. Order matches ``apps_script/Code.gs::_detectTabType``:
    ``x4`` wins over ``simple`` because "simple x4" means the 4-video flow.
    """
    name = (worksheet_name or "").lower().strip()
    if "x4" in name:
        return TAB_IMAGE_VO
    if "simple" in name:
        return TAB_SIMPLE
    if "cartoon" in name:
        return TAB_CARTOON
    return None


def infer_layout_from_headers(headers: list[str]) -> str | None:
    """Detect the row layout from the worksheet's column headers (row 1).

    Mirrors the fallback branch of ``_detectTabType``: ``how many`` ->
    four_images; ``manual image`` -> image_vo. Returns ``None`` if neither
    header is present.
    """
    normalised = [(h or "").lower().strip() for h in headers]
    if "how many" in normalised:
        return TAB_FOUR_IMAGES
    if "manual image" in normalised:
        return TAB_IMAGE_VO
    return None


async def detect_layout(
    sheets: SheetsClient, sheet_id: str, worksheet_name: str
) -> str:
    """Auto-detect the row layout for a worksheet. Tries the name first
    (no API call); falls back to reading row 1 for header-based detection.
    Raises ``ValueError`` with an actionable message if both fail."""
    from_name = infer_layout_from_name(worksheet_name)
    if from_name is not None:
        _log.info(
            "run_local_layout_detected", source="name", worksheet=worksheet_name,
            layout=from_name,
        )
        return from_name

    headers = await sheets.read_header_row(sheet_id, worksheet_name)
    from_headers = infer_layout_from_headers(headers)
    if from_headers is not None:
        _log.info(
            "run_local_layout_detected", source="headers", worksheet=worksheet_name,
            layout=from_headers,
        )
        return from_headers

    choices = "|".join(ALLOWED_TABS)
    raise ValueError(
        f"could not auto-detect layout for worksheet '{worksheet_name}'. "
        f"The tab name doesn't contain 'x4', 'simple', or 'cartoon', and row 1 "
        f"has no 'How Many' or 'Manual Image' header. "
        f"Re-run with an explicit --layout {{{choices}}}."
    )


# ── Row-range parser ────────────────────────────────────────────────────────


def parse_row_range(spec: str) -> list[int]:
    """Parse a 1-indexed sheet-row range spec like ``"5,7,9-12"``.

    Returns a sorted, deduped list. Rejects 0, negatives, reversed ranges
    (``9-5``), and any non-numeric token, naming the bad token in the error.
    """
    if not spec or not spec.strip():
        raise ValueError("--rows is empty; expected e.g. 5,7,9-12")

    out: set[int] = set()
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            raise ValueError(f"--rows has an empty token in '{spec}'")

        if "-" in token:
            parts = token.split("-")
            if len(parts) != 2:
                raise ValueError(f"--rows token '{token}' is not a valid range")
            try:
                start = int(parts[0])
                end = int(parts[1])
            except ValueError as e:
                raise ValueError(f"--rows token '{token}' is not numeric") from e
            if start < 1 or end < 1:
                raise ValueError(
                    f"--rows token '{token}' must be >= 1 (sheet rows are 1-indexed)"
                )
            if end < start:
                raise ValueError(
                    f"--rows token '{token}' is reversed; use '{end}-{start}' instead"
                )
            out.update(range(start, end + 1))
        else:
            try:
                n = int(token)
            except ValueError as e:
                raise ValueError(f"--rows token '{token}' is not numeric") from e
            if n < 1:
                raise ValueError(
                    f"--rows token '{token}' must be >= 1 (sheet rows are 1-indexed)"
                )
            out.add(n)
    return sorted(out)


# ── Tab dispatch ────────────────────────────────────────────────────────────


async def read_rows_for_tab(
    sheets: SheetsClient, tab: str, sheet_id: str, worksheet_name: str
) -> list[object]:
    """Read all rows from the named worksheet, returning the typed row objects
    matching the chosen tab. Simple shares the Image-VO column layout, so we
    read via ``read_image_vo_rows`` and convert each row to ``SimpleRow``."""
    if tab == TAB_IMAGE_VO:
        return list(await sheets.read_image_vo_rows(sheet_id, worksheet_name))
    if tab == TAB_FOUR_IMAGES:
        return list(await sheets.read_four_images_rows(sheet_id, worksheet_name))
    if tab == TAB_CARTOON:
        return list(await sheets.read_cartoon_rows(sheet_id, worksheet_name))
    if tab == TAB_SIMPLE:
        image_rows: list[ImageVORow] = list(
            await sheets.read_image_vo_rows(sheet_id, worksheet_name)
        )
        return [
            SimpleRow(
                row_num=r.row_num,
                country=r.country,
                vertical=r.vertical,
                article_url=r.article_url,
                manual_image_url=r.manual_image_url,
                voice_over=r.voice_over,
                zapcap=r.zapcap,
                aspect_ratio=r.aspect_ratio,
                script_pattern=r.script_pattern,
                open_comments=r.open_comments,
            )
            for r in image_rows
        ]
    raise ValueError(f"Unknown tab '{tab}'; expected one of {ALLOWED_TABS}")


async def process_one_row(
    tab: str, row: object, clients: PipelineClients, *, job_id: str
) -> RowResult:
    """Dispatch a typed row to its per-row pipeline function."""
    if tab == TAB_IMAGE_VO:
        return await process_image_vo_row(row, clients, job_id=job_id)    # type: ignore[arg-type]
    if tab == TAB_FOUR_IMAGES:
        return await process_4images_vo2_row(row, clients, job_id=job_id)    # type: ignore[arg-type]
    if tab == TAB_SIMPLE:
        return await process_simple_row(row, clients, job_id=job_id)    # type: ignore[arg-type]
    if tab == TAB_CARTOON:
        return await process_cartoon_row(row, clients, job_id=job_id)    # type: ignore[arg-type]
    raise ValueError(f"Unknown tab '{tab}'; expected one of {ALLOWED_TABS}")


def pending_write_from_result(
    *, sheet_id: str, worksheet: str, tab: str, job_id: str, result: RowResult
) -> PendingWrite:
    """Wrap a RowResult into the shape the sheet-write batch API expects."""
    return PendingWrite(
        job_id=job_id,
        sheet_id=sheet_id,
        worksheet=worksheet,
        tab_type=tab,
        row_num=result.row_num,
        video_urls=list(result.video_urls),
        status=result.status,
        error=result.error,
    )


# ── Argv parsing ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedArgs:
    # None means "fall back to settings.BULKVID_DEFAULT_SHEET_ID at runtime".
    sheet_id: str | None
    # None means "prompt the user interactively to pick a tab".
    worksheet: str | None
    # None means "auto-detect at runtime" from worksheet name + row-1 headers.
    tab: str | None
    # None means "default to all unprocessed rows in the chosen worksheet"
    # (and, when running interactively, prompt the user before committing).
    row_nums: list[int] | None
    concurrency: int | None
    log_file: Path | None
    dry_run: bool


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_local",
        description=(
            "Process bulk-video Sheet rows on this machine, bypassing "
            "PythonAnywhere. Reads rows from the named worksheet, runs the same "
            "pipeline PA runs, writes results back to the same cells."
        ),
    )
    p.add_argument(
        "--sheet-id",
        required=False,
        default=None,
        help=(
            "Google Sheet ID (the long token in the spreadsheet URL). "
            "If omitted, falls back to BULKVID_DEFAULT_SHEET_ID from .env. "
            "Pass this only when working against a different sheet."
        ),
    )
    p.add_argument(
        "--worksheet",
        required=False,
        default=None,
        help=(
            "Tab NAME at the bottom of the Sheet (case + spaces exact). "
            "If omitted, the script shows a numbered menu of the spreadsheet's "
            "tabs and lets you pick one interactively."
        ),
    )
    p.add_argument(
        "--layout",
        required=False,
        default=None,
        choices=ALLOWED_TABS,
        help=(
            "Optional override for the row LAYOUT (column structure). If "
            "omitted, the script auto-detects from the worksheet name "
            "('x4'->image_vo, 'simple'->simple, 'cartoon'->cartoon) and falls "
            "back to row-1 headers ('How Many'->four_images_vo2, 'Manual "
            "Image'->image_vo). Pass this only when auto-detection picks the "
            "wrong layout."
        ),
    )
    p.add_argument(
        "--rows",
        required=False,
        default=None,
        help=(
            "1-indexed sheet rows to process, e.g. 5  or  5,7  or  5,7,9-12. "
            "If omitted, defaults to ALL unprocessed rows in the chosen "
            "worksheet (rows whose 'Ready Video 1' cell is empty)."
        ),
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Max rows in flight. Defaults to BULKVID_MAX_CONCURRENT_ROWS from .env.",
    )
    p.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Optional path to also tee logs into a file (stdout is always used).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse args + read the Sheet, then exit without calling any vendor.",
    )
    return p


def parse_argv(argv: Sequence[str]) -> ParsedArgs:
    """Parse CLI args. Raises SystemExit on bad input (argparse default) so
    the user gets a clean error line instead of a Python traceback."""
    ns = _build_arg_parser().parse_args(list(argv))
    row_nums: list[int] | None
    if ns.rows is None:
        row_nums = None
    else:
        try:
            row_nums = parse_row_range(ns.rows)
        except ValueError as e:
            raise SystemExit(f"error: {e}") from e
    if ns.concurrency is not None and ns.concurrency < 1:
        raise SystemExit("error: --concurrency must be >= 1")
    return ParsedArgs(
        sheet_id=ns.sheet_id,
        worksheet=ns.worksheet,
        tab=ns.layout,
        row_nums=row_nums,
        concurrency=ns.concurrency,
        log_file=ns.log_file,
        dry_run=ns.dry_run,
    )


# ── Prerequisite validation ─────────────────────────────────────────────────


def validate_prereqs(settings: Settings) -> list[str]:
    """Return a list of missing-prereq messages. Empty list = good to go.

    Google Sheets credentials are accepted in either form — a JSON file path
    (``SHEETS_SERVICE_ACCOUNT_FILE``) OR the inline ``GOOGLE_*`` env vars
    (``GOOGLE_PRIVATE_KEY`` + ``GOOGLE_CLIENT_EMAIL`` + friends). This mirrors
    ``sheets.build_client_from_settings`` so the validator and the constructor
    agree on what counts as configured.
    """
    errors: list[str] = []

    if settings.SHEETS_SERVICE_ACCOUNT_FILE:
        if not Path(settings.SHEETS_SERVICE_ACCOUNT_FILE).is_file():
            errors.append(
                f"SHEETS_SERVICE_ACCOUNT_FILE points to "
                f"'{settings.SHEETS_SERVICE_ACCOUNT_FILE}' but the file does not exist"
            )
    elif build_credentials_info(settings) is None:
        errors.append(
            "no Google Sheets credentials in .env — set either "
            "SHEETS_SERVICE_ACCOUNT_FILE to a JSON path, or the inline "
            "GOOGLE_* env vars (GOOGLE_PRIVATE_KEY, GOOGLE_CLIENT_EMAIL, "
            "GOOGLE_PROJECT_ID, etc.)"
        )

    if not settings.OPENAI_API_KEY:
        errors.append("OPENAI_API_KEY is empty in .env")
    if not settings.kie_key_list:
        errors.append(
            "KIE_AI_KEYS is empty in .env (one or more comma-separated keys required)"
        )
    if not settings.RENDI_API_KEY:
        errors.append("RENDI_API_KEY is empty in .env")
    return errors


# ── Interactive prompts (used when --worksheet / --rows are omitted) ───────


def _is_interactive() -> bool:
    """True if stdin is a real terminal that can prompt the user. Returns
    False under pytest capture, piped input, or PyCharm's basic Run console
    without 'Emulate terminal in output console' enabled — in which case we
    refuse to prompt and ask the caller to pass args explicitly."""
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def pick_worksheet_from_menu(
    worksheet_names: list[str], *, input_fn=input
) -> str:
    """Show a numbered menu of worksheets and return the chosen name.

    Accepts either a number (``1``, ``2``, …) or the exact tab name
    (case-insensitive when unambiguous). Typing ``q`` exits cleanly via
    ``SystemExit(0)``. ``input_fn`` is injectable so tests can drive this
    without a real terminal.
    """
    if not worksheet_names:
        raise ValueError("the spreadsheet has no worksheet tabs")

    print("\nAvailable tabs:")
    for i, n in enumerate(worksheet_names, start=1):
        print(f"  {i}. {n}")
    print("  q. quit")

    while True:
        choice = input_fn("\nPick a tab (number or name): ").strip()
        if choice.lower() == "q":
            raise SystemExit(0)

        # Numeric choice.
        try:
            idx = int(choice)
        except ValueError:
            idx = None
        if idx is not None and 1 <= idx <= len(worksheet_names):
            return worksheet_names[idx - 1]

        # Exact name.
        if choice in worksheet_names:
            return choice

        # Case-insensitive unique match.
        ci_matches = [
            n for n in worksheet_names if n.lower() == choice.lower()
        ]
        if len(ci_matches) == 1:
            return ci_matches[0]

        print(
            f"  not a valid choice — pick 1-{len(worksheet_names)} or the "
            f"exact tab name (or 'q' to quit)"
        )


def pick_rows_from_default(
    unprocessed: list[int], *, input_fn=input
) -> list[int]:
    """Show the unprocessed-row count and accept either Enter (take all) or
    a row range to override. ``q`` exits via ``SystemExit(0)``. ``input_fn``
    is injectable for tests."""
    if not unprocessed:
        # Caller decides what to do with an empty set — we just signal that
        # there's nothing pending.
        return []

    sample = ", ".join(str(n) for n in unprocessed[:20])
    more = f", ...+{len(unprocessed) - 20} more" if len(unprocessed) > 20 else ""
    print(f"\n{len(unprocessed)} unprocessed row(s): {sample}{more}")

    while True:
        choice = input_fn(
            "[Press Enter to process all of them, or enter a row range like "
            "5,7,9-12 to override; 'q' to quit]: "
        ).strip()
        if choice.lower() == "q":
            raise SystemExit(0)
        if not choice:
            return list(unprocessed)
        try:
            return parse_row_range(choice)
        except ValueError as e:
            print(f"  {e}")


# ── Job-id + log-file helpers ───────────────────────────────────────────────


def make_job_id() -> str:
    """Build a unique-per-invocation id used as the structlog batch_id and the
    per-job log filename. Mirrors PA's queue-job-id shape so the logs look the
    same on both sides."""
    host = socket.gethostname() or "local"
    host_clean = "".join(c if c.isalnum() else "-" for c in host).strip("-").lower()
    if not host_clean:
        host_clean = "local"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"local-{host_clean}-{ts}"


def _attach_log_file(path: Path) -> None:
    """Mirror stdlib logger output to ``path``. structlog routes through stdlib
    logging, so a FileHandler captures the same JSON lines stdout receives."""
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)


def summary_line(r: RowResult) -> str:
    """Human-readable one-line summary printed to stdout per finished row."""
    if r.status == STATUS_SUCCESS:
        videos = len(r.video_urls)
        return (
            f"Row {r.row_num}: SUCCESS · {videos} video"
            f"{'s' if videos != 1 else ''} · "
            f"${(r.cost_usd or 0.0):.4f} · {(r.elapsed_seconds or 0.0):.1f}s"
        )
    err = (r.error or "").replace("\n", " ")[:120]
    return f'Row {r.row_num}: FAILED {r.status} · "{err}"'


# ── Per-row handler + batch runner ──────────────────────────────────────────


async def _handle_row(
    row: object,
    *,
    tab: str,
    clients: PipelineClients,
    sheets: SheetsClient,
    sheet_id: str,
    worksheet: str,
    job_id: str,
    sem: asyncio.Semaphore,
) -> RowResult:
    """Process one row under the concurrency semaphore, then write the result
    back to the Sheet immediately. Never raises — every failure is mapped to a
    RowResult with a non-SUCCESS status so the batch summary stays consistent."""
    async with sem:
        row_num = int(getattr(row, "row_num"))
        set_context(batch_id=job_id, row_num=row_num)
        _log.info("run_local_row_dispatch", row_num=row_num, tab=tab, job_id=job_id)
        try:
            result = await process_one_row(tab, row, clients, job_id=job_id)
        except Exception as e:
            _log.exception(
                "run_local_row_unhandled", row_num=row_num, error=str(e)[:300]
            )
            result = RowResult(
                row_num=row_num,
                status=STATUS_INTERNAL_ERROR,
                error=f"unhandled: {str(e)[:300]}",
            )

        _log.info(
            "run_local_row_done",
            row_num=result.row_num,
            status=result.status,
            videos=len(result.video_urls),
            cost_usd=round(result.cost_usd or 0.0, 4),
            elapsed_seconds=round(result.elapsed_seconds or 0.0, 2),
            error=(result.error or "")[:200] if result.error else None,
        )
        print(summary_line(result), flush=True)

        try:
            written = await sheets.batch_write_video_urls(
                [
                    pending_write_from_result(
                        sheet_id=sheet_id,
                        worksheet=worksheet,
                        tab=tab,
                        job_id=job_id,
                        result=result,
                    )
                ]
            )
            _log.info(
                "run_local_writeback", row_num=result.row_num, cells_written=written
            )
        except Exception as e:
            _log.exception(
                "run_local_writeback_failed",
                row_num=result.row_num,
                error=str(e)[:300],
            )
            print(
                f"warning: row {result.row_num} sheet write-back failed — "
                f"the row result is logged but the cell is empty. {str(e)[:200]}",
                file=sys.stderr,
                flush=True,
            )
        return result


@dataclass
class BatchOutcome:
    succeeded: int
    failed: int
    total_cost_usd: float
    elapsed_seconds: float


async def run_batch(
    rows: list[object],
    *,
    tab: str,
    clients: PipelineClients,
    sheets: SheetsClient,
    sheet_id: str,
    worksheet: str,
    job_id: str,
    concurrency: int,
) -> BatchOutcome:
    """Gather all per-row handlers under a single semaphore. Tasks that raise
    are still surfaced via gather(return_exceptions=False) — the handler
    converts every internal failure to a RowResult before returning, so an
    actual exception out of here is a real bug, not a row failure."""
    sem = asyncio.Semaphore(concurrency)
    start = time.monotonic()
    tasks = [
        asyncio.create_task(
            _handle_row(
                r,
                tab=tab,
                clients=clients,
                sheets=sheets,
                sheet_id=sheet_id,
                worksheet=worksheet,
                job_id=job_id,
                sem=sem,
            )
        )
        for r in rows
    ]
    results: list[RowResult] = await asyncio.gather(*tasks)
    elapsed = time.monotonic() - start

    succeeded = sum(1 for r in results if r.status == STATUS_SUCCESS)
    failed = len(results) - succeeded
    total_cost = sum(r.cost_usd or 0.0 for r in results)
    return BatchOutcome(
        succeeded=succeeded,
        failed=failed,
        total_cost_usd=total_cost,
        elapsed_seconds=elapsed,
    )


# ── Main async entry ────────────────────────────────────────────────────────


async def _run(args: ParsedArgs) -> int:
    configure_logging()
    if args.log_file is not None:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        _attach_log_file(args.log_file)

    settings = get_settings()

    prereq_errors = validate_prereqs(settings)
    if prereq_errors:
        for e in prereq_errors:
            _log.error("run_local_prereq_missing", problem=e)
            print(f"error: {e}", file=sys.stderr)
        print(
            "\nFix the .env file (see tools/README.md) and rerun.", file=sys.stderr
        )
        return 2

    # ── Resolve the sheet ID (CLI value, else .env default) ─────────────────
    sheet_id_source = "cli" if args.sheet_id else "env"
    sheet_id = args.sheet_id or settings.BULKVID_DEFAULT_SHEET_ID
    if not sheet_id:
        _log.error("run_local_sheet_id_missing")
        print(
            "error: no sheet ID given. Pass --sheet-id, or set "
            "BULKVID_DEFAULT_SHEET_ID in .env so it's remembered.",
            file=sys.stderr,
        )
        return 2

    job_id = make_job_id()
    concurrency = args.concurrency or settings.BULKVID_MAX_CONCURRENT_ROWS
    set_context(batch_id=job_id)

    _log.info(
        "run_local_start",
        job_id=job_id,
        layout_override=args.tab,    # None means "auto-detect"
        sheet_id=sheet_id,
        sheet_id_source=sheet_id_source,
        worksheet=args.worksheet,    # None means "prompt to pick"
        rows_requested=(
            len(args.row_nums) if args.row_nums is not None else None
        ),    # None means "default to unprocessed"
        concurrency=concurrency,
        dry_run=args.dry_run,
    )

    data_dir = Path(settings.BULKVID_DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    settings_store = SettingsStore(
        data_dir / "settings.db", defaults=registry_defaults()
    )
    settings_store.migrate_legacy_keys_sync(
        {
            SETTING_SCRIPT_SYSTEM_PROMPT: (
                SETTING_SIMPLE_SCRIPT_PROMPT,
                SETTING_SIMPLE_X4_SCRIPT_PROMPT,
            ),
        }
    )

    sheets = build_sheets_client(settings)
    clients = build_pipeline_clients(settings)
    clients.settings_store = settings_store

    _log.info(
        "run_local_settings_loaded",
        kie_keys_configured=len(settings.kie_key_list),
        zapcap_configured=bool(settings.ZAPCAP_API_KEY),
        atlas_configured=bool(settings.ATLAS_API_KEY),
        rendi_configured=bool(settings.RENDI_API_KEY),
        openai_configured=bool(settings.OPENAI_API_KEY),
        tavily_configured=bool(settings.TAVILY_API_KEY),
        scrapingbee_configured=bool(settings.SCRAPINGBEE_API_KEY),
    )

    # ── Resolve the worksheet (CLI value, or interactive picker) ────────────
    worksheet = args.worksheet
    if worksheet is None:
        if not _is_interactive():
            print(
                "error: --worksheet is required when running without a "
                "terminal (e.g. under CI or a piped invocation).",
                file=sys.stderr,
            )
            settings_store.close()
            return 2
        try:
            tab_names = await sheets.list_worksheets(sheet_id)
        except Exception as e:
            msg = str(e)
            _log.error("run_local_list_worksheets_failed", error=msg[:300])
            print(f"error: could not list sheet tabs — {msg[:200]}", file=sys.stderr)
            print(
                "Verify the sheet is shared with the service-account email.",
                file=sys.stderr,
            )
            settings_store.close()
            return 2
        try:
            worksheet = pick_worksheet_from_menu(tab_names)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            settings_store.close()
            return 2
        _log.info("run_local_worksheet_picked", worksheet=worksheet)

    # ── Resolve the row layout ──────────────────────────────────────────────
    # Explicit --layout wins; otherwise auto-detect from the worksheet name +
    # row-1 headers (mirrors apps_script/Code.gs::_detectTabType).
    if args.tab is not None:
        layout = args.tab
        _log.info(
            "run_local_layout_resolved", source="cli", layout=layout,
            worksheet=worksheet,
        )
    else:
        try:
            layout = await detect_layout(sheets, sheet_id, worksheet)
        except ValueError as e:
            _log.error("run_local_layout_detection_failed", error=str(e))
            print(f"error: {e}", file=sys.stderr)
            settings_store.close()
            return 2
        except Exception as e:
            msg = str(e)
            _log.error("run_local_sheet_read_failed", error=msg[:300])
            print(f"error: could not read sheet — {msg[:200]}", file=sys.stderr)
            print(
                f"Verify the sheet is shared with the service-account email and "
                f"the worksheet name '{worksheet}' is exact (case + spaces).",
                file=sys.stderr,
            )
            settings_store.close()
            return 2

    # ── Read input rows + processed-row set in parallel ─────────────────────
    try:
        all_rows = await read_rows_for_tab(
            sheets, layout, sheet_id, worksheet
        )
        processed_nums = await sheets.read_processed_row_nums(
            sheet_id, worksheet, layout=layout
        )
    except Exception as e:
        msg = str(e)
        _log.error("run_local_sheet_read_failed", error=msg[:300])
        print(f"error: could not read sheet — {msg[:200]}", file=sys.stderr)
        print(
            f"Verify the sheet is shared with the service-account email and the "
            f"worksheet name '{worksheet}' is exact (case + spaces).",
            file=sys.stderr,
        )
        settings_store.close()
        return 2

    found_nums = {int(getattr(r, "row_num")) for r in all_rows}
    unprocessed_nums = sorted(found_nums - processed_nums)

    # ── Resolve which row numbers to process ────────────────────────────────
    if args.row_nums is not None:
        # Explicit --rows. Warn if any selected rows already have a video.
        requested = sorted(set(args.row_nums))
        overwrite = [n for n in requested if n in processed_nums]
        if overwrite:
            print(
                f"\nwarning: row(s) {', '.join(str(n) for n in overwrite)} "
                f"already have a video — their cells WILL BE OVERWRITTEN.",
                flush=True,
            )
    else:
        # Default: all unprocessed rows. If we're interactive, let the user
        # override the default before committing.
        if not unprocessed_nums:
            print(
                f"\nNo unprocessed rows in worksheet '{worksheet}' "
                f"(layout '{layout}'). Nothing to do.",
                flush=True,
            )
            _log.info(
                "run_local_no_unprocessed", worksheet=worksheet, layout=layout
            )
            settings_store.close()
            return 0
        if _is_interactive():
            try:
                requested = pick_rows_from_default(unprocessed_nums)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                settings_store.close()
                return 2
        else:
            requested = list(unprocessed_nums)
            print(
                f"\nProcessing all {len(requested)} unprocessed row(s) in "
                f"'{worksheet}' (layout '{layout}').",
                flush=True,
            )

    requested_set = set(requested)
    matched = [
        r for r in all_rows if int(getattr(r, "row_num")) in requested_set
    ]
    missing = sorted(requested_set - found_nums)

    _log.info(
        "run_local_sheet_read",
        tab=layout,
        worksheet=worksheet,
        rows_in_sheet=len(all_rows),
        rows_already_processed=len(processed_nums),
        rows_requested=len(requested),
        rows_matched=len(matched),
        rows_requested_not_in_sheet=missing,
    )
    if missing:
        print(
            f"warning: requested rows not found in worksheet "
            f"'{worksheet}': {', '.join(str(n) for n in missing)}",
            file=sys.stderr,
        )
    if not matched:
        print(
            "error: no requested rows matched anything in the sheet — nothing to do.",
            file=sys.stderr,
        )
        settings_store.close()
        return 2

    if args.dry_run:
        for r in matched:
            print(f"would process: row {int(getattr(r, 'row_num'))}")
        _log.info("run_local_dry_run_exit", rows_planned=len(matched))
        settings_store.close()
        return 0

    print(
        f"\nProcessing {len(matched)} row(s) with layout '{layout}' "
        f"(concurrency={concurrency}, job_id={job_id})\n",
        flush=True,
    )
    interrupted = False
    try:
        outcome = await run_batch(
            matched,
            tab=layout,
            clients=clients,
            sheets=sheets,
            sheet_id=sheet_id,
            worksheet=worksheet,
            job_id=job_id,
            concurrency=concurrency,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        interrupted = True
        _log.warning("run_local_keyboard_interrupt")
        outcome = BatchOutcome(
            succeeded=0, failed=0, total_cost_usd=0.0, elapsed_seconds=0.0
        )

    settings_store.close()

    _log.info(
        "run_local_shutdown",
        succeeded=outcome.succeeded,
        failed=outcome.failed,
        total_cost_usd=round(outcome.total_cost_usd, 4),
        elapsed_seconds=round(outcome.elapsed_seconds, 2),
        interrupted=interrupted,
    )
    print(
        f"\nDone — {outcome.succeeded} succeeded, {outcome.failed} failed, "
        f"${outcome.total_cost_usd:.4f} total, "
        f"{outcome.elapsed_seconds:.1f}s elapsed"
        + (" (interrupted)" if interrupted else "")
    )
    if interrupted:
        return 130
    return 0 if outcome.failed == 0 else 1


# ── Sync entrypoint ─────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_argv(argv if argv is not None else sys.argv[1:])
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
