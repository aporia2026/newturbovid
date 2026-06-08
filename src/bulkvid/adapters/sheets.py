"""Google Sheets adapter — gspread-based read + batch write.

Two reads:
  - ``read_image_vo_rows``    -> list[ImageVORow]      from the Image-VO tab
  - ``read_four_images_rows`` -> list[FourImagesVO2Row] from the 4Images-VO2 tab

One write surface:
  - ``batch_write_video_urls(writes)`` — coalesced flush callback used by
    ``CoalescedSheetWriter``. Groups writes by (sheet_id, worksheet) and
    issues one ``batch_update`` per destination.

gspread is sync; every public method wraps the work in ``asyncio.to_thread``
so the runner's event loop stays responsive.

Plan §15 Appendix A (column maps), §5 (Concurrency model: 5s coalesced writes).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from bulkvid.adapters._retry import with_retry
from bulkvid.config import Settings, get_settings
from bulkvid.logging import get_logger
from bulkvid.models.row import (
    CardChoice,
    CartoonRow,
    FourImagesVO2Row,
    ImageVORow,
    SimpleX4Row,
)
from bulkvid.orchestrator.queue import (
    TAB_CARTOON,
    TAB_FOUR_IMAGES,
    TAB_IMAGE_VO,
    TAB_SIMPLE,
    TAB_SIMPLE_X4,
)
from bulkvid.orchestrator.sheet_writer import PendingWrite

_log = get_logger("sheets")


# ── Errors ──────────────────────────────────────────────────────────────────


class SheetsError(RuntimeError):
    """Base class for adapter-level Sheets errors."""


class SheetsRateLimitError(SheetsError):
    """Google Sheets quota (429). Retryable."""


class SheetsServerError(SheetsError):
    """5xx from the Sheets API. Retryable."""


class SheetsConnectionError(SheetsError):
    """Network died before the Sheets call landed. Retryable."""


def _status_from_gspread_error(exc: BaseException) -> int | None:
    """Pull the HTTP status off a gspread ``APIError`` shape if possible.

    gspread wraps Google API errors in ``APIError`` which carries a
    ``response`` attribute with ``status_code``. Older versions stash it
    differently; we degrade gracefully so the classifier still works.
    """
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    return None


def _classify_sheets_error(exc: BaseException) -> BaseException:
    """Map gspread / google-api errors to our local hierarchy.

    Returns a fresh wrapped exception when the underlying error is retryable;
    returns the original exception unchanged when terminal.
    """
    status = _status_from_gspread_error(exc)
    msg = str(exc).lower()

    if status == 429 or "rate limit" in msg or "quota" in msg:
        return SheetsRateLimitError(str(exc))
    if status in {500, 502, 503, 504} or any(
        code in msg for code in (" 500 ", " 502 ", " 503 ", " 504 ")
    ):
        return SheetsServerError(str(exc))
    # Connection-style failures from googleapiclient / underlying urllib3.
    if type(exc).__name__ in {"ConnectionError", "Timeout", "ReadTimeoutError"}:
        return SheetsConnectionError(str(exc))
    return exc


GSHEETS_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
)


# ── Column maps (0-indexed; plan §15 Appendix A) ────────────────────────────


@dataclass(frozen=True)
class _ImageVOCols:
    country: int = 0          # A
    vertical: int = 1         # B
    article: int = 2          # C
    manual_image: int = 3     # D
    voice_over: int = 4       # E
    zapcap: int = 5           # F
    aspect_ratio: int = 6     # G
    script_pattern: int = 7   # H
    open_comments: int = 8    # I
    ready_video_start: int = 9   # J = 0-indexed col 9


@dataclass(frozen=True)
class _FourImagesCols:
    country: int = 0          # A
    vertical: int = 1         # B
    article: int = 2          # C
    how_many: int = 3         # D
    voice_over: int = 4       # E
    image1: int = 5           # F
    image2: int = 6           # G
    image3: int = 7           # H
    image4: int = 8           # I
    zapcap: int = 9           # J
    aspect_ratio: int = 10    # K
    script_pattern: int = 11  # L
    open_comments: int = 12   # M
    ready_video_start: int = 13   # N = 0-indexed col 13


IMAGE_VO_COLS = _ImageVOCols()
FOUR_IMAGES_COLS = _FourImagesCols()


@dataclass(frozen=True)
class _SimpleX4Cols:
    """Layout for the ``simple x4`` tab after the 2026-06-08 migration.

    Inherits A-H from the Image-VO layout, inserts 8 columns for per-video
    Template + CTA pairs, then shifts Open Comments + Ready Video 1-4
    right by 8. Plan ``_plans/2026-06-08-simple-x4-template-cards.md`` §D.1.
    """

    country: int = 0          # A
    vertical: int = 1         # B
    article: int = 2          # C
    manual_image: int = 3     # D
    voice_over: int = 4       # E
    zapcap: int = 5           # F
    aspect_ratio: int = 6     # G
    script_pattern: int = 7   # H
    template_1: int = 8       # I  (NEW)
    cta_1: int = 9            # J  (NEW)
    template_2: int = 10      # K  (NEW)
    cta_2: int = 11           # L  (NEW)
    template_3: int = 12      # M  (NEW)
    cta_3: int = 13           # N  (NEW)
    template_4: int = 14      # O  (NEW)
    cta_4: int = 15           # P  (NEW)
    open_comments: int = 16   # Q  (shifted from 8)
    ready_video_start: int = 17   # R = 0-indexed col 17 (shifted from 9)


SIMPLE_X4_COLS = _SimpleX4Cols()


@dataclass(frozen=True)
class _CartoonCols:
    """Layout for the ``cartoon`` tab after the 2026-06-08 CTA column insertion.

    Inherits A-H from the Image-VO layout (Manual Image at D is ignored for
    cartoon — scenes are generated from scratch), inserts 2 columns for the
    operator's CTA toggle + text, then shifts Open Comments + Ready Video 1/2
    right by 2. Cartoon only writes back TWO Ready Video URLs (the row produces
    two ideas, not four), so ready_video_start covers cols L + M only.
    """

    country: int = 0          # A
    vertical: int = 1         # B
    article: int = 2          # C
    manual_image: int = 3     # D  (ignored — cartoon generates from scratch)
    voice_over: int = 4       # E
    zapcap: int = 5           # F
    aspect_ratio: int = 6     # G
    script_pattern: int = 7   # H
    cta_enabled: int = 8      # I  (NEW — Yes/No dropdown)
    cta_text: int = 9         # J  (NEW — operator text; empty → per-language default)
    open_comments: int = 10   # K  (shifted from 8)
    ready_video_start: int = 11   # L = 0-indexed col 11 (shifted from 9)


CARTOON_COLS = _CartoonCols()


# Header rows BEFORE data starts.
#   - Image-VO / Simple / Cartoon / 4Images: 1 header row → data at sheet row 2
#   - Simple x4 (post-migration): 2 header rows (row 1 = template previews,
#     row 2 = column names) → data at sheet row 3
#
# Centralised so the readers (read_*_rows, read_processed_row_nums) agree.
_HEADER_ROWS_BY_TAB: dict[str, int] = {
    TAB_IMAGE_VO: 1,
    TAB_FOUR_IMAGES: 1,
    TAB_SIMPLE: 1,
    TAB_CARTOON: 1,
    TAB_SIMPLE_X4: 2,
}


# ── Cell parsing helpers ────────────────────────────────────────────────────


def _cell(row: list[str], idx: int, default: str = "") -> str:
    if idx >= len(row):
        return default
    val = row[idx]
    return val.strip() if val else default


def _yes(value: str, *, default: bool = True) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return default
    return v in ("yes", "y", "true", "1")


def _safe_int(value: str, *, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _row_is_blank(row: list[str], required_idxs: list[int]) -> bool:
    """A row is "blank" if every required column is empty."""
    return all(not _cell(row, i) for i in required_idxs)


# ── Adapter ─────────────────────────────────────────────────────────────────


class SheetsClient:
    """Async wrapper over gspread.

    Credentials can come from any of three sources, in priority order:
      1. A pre-built gspread ``client`` (tests inject one here).
      2. ``credentials_file`` — path to a service-account JSON on disk.
         Preferred when the host has a filesystem we can drop a JSON onto
         (Hetzner Docker volume, local dev machines).
      3. ``credentials_info`` — the parsed JSON shape as a dict. Used when
         the JSON contents live in env vars instead of a file
         (PythonAnywhere-friendly, mirrors how ``google_credentials.py``
         feeds the other Google services).
    """

    def __init__(
        self,
        credentials_file: str = "",
        *,
        credentials_info: dict[str, Any] | None = None,
        client: Any | None = None,    # injectable for tests
    ) -> None:
        if client is not None:
            self._client = client
        elif credentials_file:
            creds = Credentials.from_service_account_file(
                credentials_file, scopes=list(GSHEETS_SCOPES)
            )
            self._client = gspread.authorize(creds)
        elif credentials_info is not None:
            creds = Credentials.from_service_account_info(
                credentials_info, scopes=list(GSHEETS_SCOPES)
            )
            self._client = gspread.authorize(creds)
        else:
            raise ValueError(
                "SheetsClient requires credentials_file, credentials_info, "
                "or a pre-built client"
            )

    # ── Retry wrapper around to_thread ──────────────────────────────────────

    async def _to_thread_with_retry(
        self,
        fn: Callable[..., Any],
        *args: Any,
        op: str,
        attempts: int = 3,
    ) -> Any:
        """Run a sync gspread call off-loop with retry on transient errors.

        ``attempts=3`` for reads. Writes pass ``attempts=2`` (a single retry)
        because Google's batch_update is not idempotent — re-submitting after
        a network-dropped 200 would double-write cells. One retry is the
        compromise: absorbs a single 429, doesn't double-write on a flake.
        """

        async def _call() -> Any:
            try:
                return await asyncio.to_thread(fn, *args)
            except Exception as e:
                wrapped = _classify_sheets_error(e)
                if wrapped is e:
                    raise
                raise wrapped from e

        return await with_retry(
            _call,
            op=op,
            retryable=(
                SheetsRateLimitError,
                SheetsServerError,
                SheetsConnectionError,
            ),
            attempts=attempts,
        )

    # ── Sync workers (invoked via asyncio.to_thread) ────────────────────────

    def _get_worksheet_sync(self, sheet_id: str, worksheet_name: str) -> Any:
        spreadsheet = self._client.open_by_key(sheet_id)
        return spreadsheet.worksheet(worksheet_name)

    def _read_all_values_sync(self, sheet_id: str, worksheet_name: str) -> list[list[str]]:
        ws = self._get_worksheet_sync(sheet_id, worksheet_name)
        return ws.get_all_values()

    def _read_header_row_sync(self, sheet_id: str, worksheet_name: str) -> list[str]:
        ws = self._get_worksheet_sync(sheet_id, worksheet_name)
        return ws.row_values(1)

    def _list_worksheets_sync(self, sheet_id: str) -> list[str]:
        spreadsheet = self._client.open_by_key(sheet_id)
        return [ws.title for ws in spreadsheet.worksheets()]

    def _read_processed_row_nums_sync(
        self,
        sheet_id: str,
        worksheet_name: str,
        ready_video_col_0based: int,
        header_rows: int = 1,
    ) -> set[int]:
        data = self._read_all_values_sync(sheet_id, worksheet_name)
        processed: set[int] = set()
        for offset, raw in enumerate(data[header_rows:], start=header_rows + 1):
            if _cell(raw, ready_video_col_0based):
                processed.add(offset)
        return processed

    def _batch_update_sync(
        self, sheet_id: str, worksheet_name: str, updates: list[dict]
    ) -> None:
        ws = self._get_worksheet_sync(sheet_id, worksheet_name)
        ws.batch_update(updates)

    # ── Public read API ─────────────────────────────────────────────────────

    async def read_header_row(
        self, sheet_id: str, worksheet_name: str
    ) -> list[str]:
        """Return row 1 (the column headers) as a list of strings. Used by the
        local runner's layout auto-detection so it can fall back from
        name-based detection to header-based detection without reading the
        whole sheet."""
        return await self._to_thread_with_retry(
            self._read_header_row_sync, sheet_id, worksheet_name, op="sheets read_header"
        )

    async def list_worksheets(self, sheet_id: str) -> list[str]:
        """Return every worksheet (tab) name in the spreadsheet, in tab order.
        Used by the local runner's interactive tab picker."""
        return await self._to_thread_with_retry(
            self._list_worksheets_sync, sheet_id, op="sheets list_worksheets"
        )

    async def read_processed_row_nums(
        self, sheet_id: str, worksheet_name: str, *, layout: str
    ) -> set[int]:
        """Return the set of 1-indexed sheet row numbers whose ``Ready Video 1``
        cell is non-empty — i.e. rows that have already been processed.
        Used by the local runner so the default "process all unprocessed rows"
        path can skip rows that already have output."""
        if layout in (TAB_IMAGE_VO, TAB_SIMPLE):
            col = IMAGE_VO_COLS.ready_video_start
        elif layout == TAB_CARTOON:
            col = CARTOON_COLS.ready_video_start
        elif layout == TAB_FOUR_IMAGES:
            col = FOUR_IMAGES_COLS.ready_video_start
        elif layout == TAB_SIMPLE_X4:
            col = SIMPLE_X4_COLS.ready_video_start
        else:
            return set()
        return await self._to_thread_with_retry(
            self._read_processed_row_nums_sync,
            sheet_id,
            worksheet_name,
            col,
            _HEADER_ROWS_BY_TAB.get(layout, 1),
            op="sheets read_processed",
        )

    async def read_image_vo_rows(
        self, sheet_id: str, worksheet_name: str
    ) -> list[ImageVORow]:
        data = await self._to_thread_with_retry(
            self._read_all_values_sync,
            sheet_id,
            worksheet_name,
            op="sheets read_image_vo",
        )
        rows: list[ImageVORow] = []
        if not data:
            return rows

        cols = IMAGE_VO_COLS
        required = [cols.article, cols.manual_image]

        # data[0] is the header row; data[1:] are content.
        for offset, raw in enumerate(data[1:], start=2):    # sheet rows are 1-indexed; header is row 1
            if _row_is_blank(raw, required):
                continue
            article = _cell(raw, cols.article)
            seed = _cell(raw, cols.manual_image)
            if not article or not seed:
                # Partially-populated row: skip rather than queue garbage.
                _log.warning(
                    "skip_incomplete_image_vo_row",
                    sheet_row=offset,
                    has_article=bool(article),
                    has_seed=bool(seed),
                )
                continue
            rows.append(
                ImageVORow(
                    row_num=offset,
                    country=_cell(raw, cols.country),
                    vertical=_cell(raw, cols.vertical),
                    article_url=article,
                    manual_image_url=seed,
                    voice_over=_yes(_cell(raw, cols.voice_over), default=True),
                    zapcap=_yes(_cell(raw, cols.zapcap), default=False),
                    aspect_ratio=_cell(raw, cols.aspect_ratio, default="9:16"),
                    script_pattern=_cell(raw, cols.script_pattern),
                    open_comments=_cell(raw, cols.open_comments),
                )
            )

        _log.info(
            "read_image_vo_rows",
            sheet_id=sheet_id,
            worksheet=worksheet_name,
            row_count=len(rows),
        )
        return rows

    async def read_four_images_rows(
        self, sheet_id: str, worksheet_name: str
    ) -> list[FourImagesVO2Row]:
        data = await self._to_thread_with_retry(
            self._read_all_values_sync,
            sheet_id,
            worksheet_name,
            op="sheets read_four_images",
        )
        rows: list[FourImagesVO2Row] = []
        if not data:
            return rows

        cols = FOUR_IMAGES_COLS
        for offset, raw in enumerate(data[1:], start=2):
            article = _cell(raw, cols.article)
            how_many = _safe_int(_cell(raw, cols.how_many), default=0)
            if not article or how_many < 1 or how_many > 4:
                if article or how_many:
                    _log.warning(
                        "skip_incomplete_four_images_row",
                        sheet_row=offset,
                        how_many=how_many,
                        has_article=bool(article),
                    )
                continue

            urls = [
                _cell(raw, cols.image1),
                _cell(raw, cols.image2),
                _cell(raw, cols.image3),
                _cell(raw, cols.image4),
            ]
            chosen = [u for u in urls[:how_many] if u]
            if len(chosen) != how_many:
                _log.warning(
                    "skip_missing_image_urls",
                    sheet_row=offset,
                    how_many=how_many,
                    supplied=len(chosen),
                )
                continue

            rows.append(
                FourImagesVO2Row(
                    row_num=offset,
                    country=_cell(raw, cols.country),
                    vertical=_cell(raw, cols.vertical),
                    article_url=article,
                    how_many=how_many,
                    voice_over=_yes(_cell(raw, cols.voice_over), default=True),
                    image_urls=chosen,
                    zapcap=_yes(_cell(raw, cols.zapcap), default=False),
                    aspect_ratio=_cell(raw, cols.aspect_ratio, default="9:16"),
                    script_pattern=_cell(raw, cols.script_pattern),
                    open_comments=_cell(raw, cols.open_comments),
                )
            )

        _log.info(
            "read_four_images_rows",
            sheet_id=sheet_id,
            worksheet=worksheet_name,
            row_count=len(rows),
        )
        return rows

    async def read_simple_x4_rows(
        self, sheet_id: str, worksheet_name: str
    ) -> list[SimpleX4Row]:
        """Read the ``simple x4`` tab (post-migration layout).

        Row 1 holds the template preview images; row 2 holds the column-name
        headers; row 3+ is data — so we skip ``data[:2]`` (NOT ``data[:1]``
        like the other readers). Plan §D.1.

        Per-video template values OUTSIDE ``{"", "1", "2"}`` are logged and
        coerced to empty so a typo on one cell never poisons the whole row.
        """
        data = await self._to_thread_with_retry(
            self._read_all_values_sync,
            sheet_id,
            worksheet_name,
            op="sheets read_simple_x4",
        )
        rows: list[SimpleX4Row] = []
        if not data:
            return rows

        cols = SIMPLE_X4_COLS
        required = [cols.article, cols.manual_image]
        header_rows = _HEADER_ROWS_BY_TAB[TAB_SIMPLE_X4]

        for offset, raw in enumerate(data[header_rows:], start=header_rows + 1):
            if _row_is_blank(raw, required):
                continue
            article = _cell(raw, cols.article)
            seed = _cell(raw, cols.manual_image)
            if not article or not seed:
                _log.warning(
                    "skip_incomplete_simple_x4_row",
                    sheet_row=offset,
                    has_article=bool(article),
                    has_seed=bool(seed),
                )
                continue

            template_idxs = (cols.template_1, cols.template_2, cols.template_3, cols.template_4)
            cta_idxs = (cols.cta_1, cols.cta_2, cols.cta_3, cols.cta_4)
            cards: list[CardChoice] = []
            for i, (t_idx, c_idx) in enumerate(zip(template_idxs, cta_idxs, strict=True)):
                raw_template = _cell(raw, t_idx)
                template_id = raw_template if raw_template in ("", "1", "2") else ""
                if raw_template and template_id == "":
                    _log.warning(
                        "simple_x4_bad_template_value",
                        sheet_row=offset,
                        video_index=i + 1,
                        value=raw_template[:40],
                    )
                cards.append(
                    CardChoice(
                        template_id=template_id,
                        cta=_cell(raw, c_idx)[:80],    # bound CTA at 80 chars (plan §D.8)
                    )
                )

            rows.append(
                SimpleX4Row(
                    row_num=offset,
                    country=_cell(raw, cols.country),
                    vertical=_cell(raw, cols.vertical),
                    article_url=article,
                    manual_image_url=seed,
                    voice_over=_yes(_cell(raw, cols.voice_over), default=True),
                    zapcap=_yes(_cell(raw, cols.zapcap), default=False),
                    aspect_ratio=_cell(raw, cols.aspect_ratio, default="9:16"),
                    script_pattern=_cell(raw, cols.script_pattern),
                    cards=cards,
                    open_comments=_cell(raw, cols.open_comments),
                )
            )

        _log.info(
            "read_simple_x4_rows",
            sheet_id=sheet_id,
            worksheet=worksheet_name,
            row_count=len(rows),
        )
        return rows

    async def read_cartoon_rows(
        self, sheet_id: str, worksheet_name: str
    ) -> list[CartoonRow]:
        """Read the cartoon tab. The Manual Image column (D) is ignored —
        cartoon scenes are generated from scratch — so only the article URL
        is required. Post-2026-06-08 the tab has a CTA (Yes/No) column at I
        and a CTA Text column at J; Open Comments + Ready Videos shift right
        by 2 (see ``_CartoonCols``)."""
        data = await self._to_thread_with_retry(
            self._read_all_values_sync,
            sheet_id,
            worksheet_name,
            op="sheets read_cartoon",
        )
        rows: list[CartoonRow] = []
        if not data:
            return rows

        cols = CARTOON_COLS
        for offset, raw in enumerate(data[1:], start=2):
            article = _cell(raw, cols.article)
            if not article:
                if not _row_is_blank(raw, [cols.country, cols.vertical, cols.open_comments]):
                    _log.warning("skip_incomplete_cartoon_row", sheet_row=offset)
                continue
            rows.append(
                CartoonRow(
                    row_num=offset,
                    country=_cell(raw, cols.country),
                    vertical=_cell(raw, cols.vertical),
                    article_url=article,
                    voice_over=_yes(_cell(raw, cols.voice_over), default=True),
                    zapcap=_yes(_cell(raw, cols.zapcap), default=False),
                    aspect_ratio=_cell(raw, cols.aspect_ratio, default="9:16"),
                    script_pattern=_cell(raw, cols.script_pattern),
                    cta_enabled=_yes(_cell(raw, cols.cta_enabled), default=False),
                    cta_text=_cell(raw, cols.cta_text)[:80],    # bound at 80 chars
                    open_comments=_cell(raw, cols.open_comments),
                )
            )

        _log.info(
            "read_cartoon_rows",
            sheet_id=sheet_id,
            worksheet=worksheet_name,
            row_count=len(rows),
        )
        return rows

    # ── Public write API (the coalesced flush callback) ─────────────────────

    async def batch_write_video_urls(self, writes: list[PendingWrite]) -> int:
        """Group by (sheet, worksheet) and issue one ``batch_update`` per destination.

        Returns the total number of cell updates issued.
        """
        if not writes:
            return 0

        grouped: dict[tuple[str, str, str], list[PendingWrite]] = defaultdict(list)
        for w in writes:
            grouped[(w.sheet_id, w.worksheet, w.tab_type)].append(w)

        total_cells = 0
        for (sheet_id, worksheet, tab_type), batch in grouped.items():
            ready_start = (
                IMAGE_VO_COLS.ready_video_start
                if tab_type in (TAB_IMAGE_VO, TAB_SIMPLE)
                else CARTOON_COLS.ready_video_start
                if tab_type == TAB_CARTOON
                else FOUR_IMAGES_COLS.ready_video_start
                if tab_type == TAB_FOUR_IMAGES
                else SIMPLE_X4_COLS.ready_video_start
                if tab_type == TAB_SIMPLE_X4
                else None
            )
            if ready_start is None:
                _log.error(
                    "skip_unknown_tab_type",
                    tab_type=tab_type,
                    sheet_id=sheet_id,
                    worksheet=worksheet,
                )
                continue

            updates: list[dict[str, Any]] = []
            for w in batch:
                for slot, url in enumerate(w.video_urls[:4]):
                    if not url:
                        continue
                    col_1based = ready_start + slot + 1   # gspread uses 1-indexed
                    cell = gspread.utils.rowcol_to_a1(w.row_num, col_1based)
                    updates.append({"range": cell, "values": [[url]]})

            if not updates:
                continue

            _log.info(
                "sheet_batch_write",
                sheet_id=sheet_id,
                worksheet=worksheet,
                tab_type=tab_type,
                cell_count=len(updates),
            )
            # attempts=2 → at most one retry. batch_update is NOT idempotent;
            # a 200 response that was lost on the wire and re-tried would
            # double-write cells. The trade-off: absorb a single 429 / transient
            # 500, but never blow past one attempt on a write.
            await self._to_thread_with_retry(
                self._batch_update_sync,
                sheet_id,
                worksheet,
                updates,
                op="sheets batch_update",
                attempts=2,
            )
            total_cells += len(updates)

        return total_cells


def build_client_from_settings(settings: Settings | None = None) -> SheetsClient:
    """Construct ``SheetsClient`` from settings, accepting either auth mode:
    a JSON file path (``SHEETS_SERVICE_ACCOUNT_FILE``) or the inline
    ``GOOGLE_*`` env vars used by ``google_credentials.build_credentials_info``.
    Raises ``ValueError`` only when neither is configured."""
    from bulkvid.adapters.google_credentials import build_credentials_info

    s = settings or get_settings()
    if s.SHEETS_SERVICE_ACCOUNT_FILE:
        return SheetsClient(credentials_file=s.SHEETS_SERVICE_ACCOUNT_FILE)
    info = build_credentials_info(s)
    if info is not None:
        return SheetsClient(credentials_info=info)
    raise ValueError(
        "Google Sheets credentials not configured: set "
        "SHEETS_SERVICE_ACCOUNT_FILE to a JSON path, OR set the inline "
        "GOOGLE_* env vars (GOOGLE_PRIVATE_KEY, GOOGLE_CLIENT_EMAIL, "
        "GOOGLE_PROJECT_ID, etc.)."
    )
