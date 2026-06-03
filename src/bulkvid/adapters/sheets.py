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
from dataclasses import dataclass
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from bulkvid.config import Settings, get_settings
from bulkvid.logging import get_logger
from bulkvid.models.row import FourImagesVO2Row, ImageVORow
from bulkvid.orchestrator.queue import TAB_FOUR_IMAGES, TAB_IMAGE_VO, TAB_SIMPLE
from bulkvid.orchestrator.sheet_writer import PendingWrite

_log = get_logger("sheets")


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
    """Async wrapper over gspread."""

    def __init__(
        self,
        credentials_file: str = "",
        *,
        client: Any | None = None,    # injectable for tests
    ) -> None:
        if client is not None:
            self._client = client
        elif credentials_file:
            creds = Credentials.from_service_account_file(
                credentials_file, scopes=list(GSHEETS_SCOPES)
            )
            self._client = gspread.authorize(creds)
        else:
            raise ValueError(
                "SheetsClient requires credentials_file (or a pre-built client)"
            )

    # ── Sync workers (invoked via asyncio.to_thread) ────────────────────────

    def _get_worksheet_sync(self, sheet_id: str, worksheet_name: str) -> Any:
        spreadsheet = self._client.open_by_key(sheet_id)
        return spreadsheet.worksheet(worksheet_name)

    def _read_all_values_sync(self, sheet_id: str, worksheet_name: str) -> list[list[str]]:
        ws = self._get_worksheet_sync(sheet_id, worksheet_name)
        return ws.get_all_values()

    def _batch_update_sync(
        self, sheet_id: str, worksheet_name: str, updates: list[dict]
    ) -> None:
        ws = self._get_worksheet_sync(sheet_id, worksheet_name)
        ws.batch_update(updates)

    # ── Public read API ─────────────────────────────────────────────────────

    async def read_image_vo_rows(
        self, sheet_id: str, worksheet_name: str
    ) -> list[ImageVORow]:
        data = await asyncio.to_thread(
            self._read_all_values_sync, sheet_id, worksheet_name
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
        data = await asyncio.to_thread(
            self._read_all_values_sync, sheet_id, worksheet_name
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
                else FOUR_IMAGES_COLS.ready_video_start
                if tab_type == TAB_FOUR_IMAGES
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
            await asyncio.to_thread(
                self._batch_update_sync, sheet_id, worksheet, updates
            )
            total_cells += len(updates)

        return total_cells


def build_client_from_settings(settings: Settings | None = None) -> SheetsClient:
    s = settings or get_settings()
    if not s.SHEETS_SERVICE_ACCOUNT_FILE:
        raise ValueError("SHEETS_SERVICE_ACCOUNT_FILE is empty; cannot build SheetsClient")
    return SheetsClient(credentials_file=s.SHEETS_SERVICE_ACCOUNT_FILE)
