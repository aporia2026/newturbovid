"""Tests for the ``simple x4`` sheets reader + batch-write path.

Post-migration ``simple x4`` layout has TWO header rows (row 1 = template
preview images, row 2 = column names) and 8 new columns between
``Script Pattern`` and ``Open Comments``. The reader and batch-writer both
need to know about that. These tests use the same MagicMock gspread fake
as ``test_sheets.py``.

Plan: ``_plans/2026-06-08-simple-x4-template-cards.md``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bulkvid.adapters.sheets import SIMPLE_X4_COLS, SheetsClient
from bulkvid.orchestrator.queue import TAB_SIMPLE_X4
from bulkvid.orchestrator.sheet_writer import PendingWrite


# ── Fixtures (mirror test_sheets.py for consistency) ────────────────────────


def _make_fake_client(
    sheet_data: dict[tuple[str, str], list[list[str]]],
) -> tuple[MagicMock, dict[tuple[str, str], MagicMock]]:
    worksheets: dict[tuple[str, str], MagicMock] = {}

    def _open_by_key(sheet_id: str) -> MagicMock:
        spreadsheet = MagicMock()

        def _worksheet(name: str) -> MagicMock:
            key = (sheet_id, name)
            if key not in worksheets:
                ws = MagicMock()
                ws.get_all_values = MagicMock(return_value=sheet_data.get(key, []))
                ws.batch_update = MagicMock(return_value=None)
                worksheets[key] = ws
            return worksheets[key]

        spreadsheet.worksheet = MagicMock(side_effect=_worksheet)
        return spreadsheet

    client = MagicMock()
    client.open_by_key = MagicMock(side_effect=_open_by_key)
    return client, worksheets


def _row(
    *,
    country="DE",
    vertical="Car Deals PR",
    article="https://example.com/article",
    manual_image="https://example.com/image.png",
    voice_over="Yes",
    zapcap="Yes",
    aspect_ratio="9:16",
    script_pattern="",
    template_1="",
    cta_1="",
    template_2="",
    cta_2="",
    template_3="",
    cta_3="",
    template_4="",
    cta_4="",
    open_comments="",
    ready_video_1="",
    ready_video_2="",
    ready_video_3="",
    ready_video_4="",
) -> list[str]:
    """Build one fully-populated simple_x4 data row in the post-migration layout."""
    return [
        country, vertical, article, manual_image,
        voice_over, zapcap, aspect_ratio, script_pattern,
        template_1, cta_1, template_2, cta_2,
        template_3, cta_3, template_4, cta_4,
        open_comments,
        ready_video_1, ready_video_2, ready_video_3, ready_video_4,
    ]


_PREVIEW_HEADER = ["Template Preview", "", "", "1", "2"]
_COLUMN_HEADER = [
    "Country", "Vertical", "Article", "Manual Image",
    "Voice Over", "ZapCap", "Change Size", "Script Pattern",
    "Template 1", "CTA 1", "Template 2", "CTA 2",
    "Template 3", "CTA 3", "Template 4", "CTA 4",
    "Open Comments",
    "Ready Video 1", "Ready Video 2", "Ready Video 3", "Ready Video 4",
]


# ── read_simple_x4_rows ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_data_starts_at_sheet_row_3() -> None:
    """Row 1 = preview, row 2 = headers; the first data row should be tagged
    with row_num=3."""
    data = [
        _PREVIEW_HEADER,
        _COLUMN_HEADER,
        _row(),
    ]
    client, _ = _make_fake_client({("sheet1", "simple x4"): data})
    sc = SheetsClient(client=client)

    rows = await sc.read_simple_x4_rows("sheet1", "simple x4")
    assert len(rows) == 1
    assert rows[0].row_num == 3, f"first data row should be sheet row 3, got {rows[0].row_num}"
    assert len(rows[0].cards) == 4, "every row carries exactly 4 cards"
    assert all(c.template_id == "" and c.cta == "" for c in rows[0].cards), (
        "blank Template* / CTA* cells should produce empty CardChoice entries"
    )


@pytest.mark.asyncio
async def test_template_id_outside_allowed_set_coerced_to_empty() -> None:
    """A typo like Template1 = '3' or 'maybe' must not crash the row; it gets
    silently downgraded to '' (no overlay)."""
    data = [
        _PREVIEW_HEADER,
        _COLUMN_HEADER,
        _row(template_1="3", template_2="maybe", template_3="1", template_4=""),
    ]
    client, _ = _make_fake_client({("sheet1", "simple x4"): data})
    sc = SheetsClient(client=client)

    rows = await sc.read_simple_x4_rows("sheet1", "simple x4")
    assert len(rows) == 1
    assert rows[0].cards[0].template_id == ""    # "3" -> ""
    assert rows[0].cards[1].template_id == ""    # "maybe" -> ""
    assert rows[0].cards[2].template_id == "1"   # valid passes
    assert rows[0].cards[3].template_id == ""    # blank stays blank


@pytest.mark.asyncio
async def test_cta_truncated_at_80_chars() -> None:
    long_cta = "x" * 200
    data = [
        _PREVIEW_HEADER,
        _COLUMN_HEADER,
        _row(template_1="1", cta_1=long_cta),
    ]
    client, _ = _make_fake_client({("sheet1", "simple x4"): data})
    sc = SheetsClient(client=client)

    rows = await sc.read_simple_x4_rows("sheet1", "simple x4")
    assert len(rows[0].cards[0].cta) == 80, "CTA over 80 chars should be truncated"


@pytest.mark.asyncio
async def test_blank_data_rows_are_skipped() -> None:
    """A blank required-cell row in the middle shouldn't poison the read."""
    blank_required = _row()
    blank_required[SIMPLE_X4_COLS.article] = ""
    blank_required[SIMPLE_X4_COLS.manual_image] = ""

    data = [
        _PREVIEW_HEADER,
        _COLUMN_HEADER,
        _row(),                          # row 3 — valid
        blank_required,                  # row 4 — blank, skip
        _row(country="MX"),              # row 5 — valid
    ]
    client, _ = _make_fake_client({("sheet1", "simple x4"): data})
    sc = SheetsClient(client=client)

    rows = await sc.read_simple_x4_rows("sheet1", "simple x4")
    assert [r.row_num for r in rows] == [3, 5]


@pytest.mark.asyncio
async def test_partial_row_missing_seed_image_is_skipped() -> None:
    """Article URL filled but Manual Image blank → skip with warning, don't crash."""
    bad = _row()
    bad[SIMPLE_X4_COLS.manual_image] = ""

    data = [_PREVIEW_HEADER, _COLUMN_HEADER, bad]
    client, _ = _make_fake_client({("sheet1", "simple x4"): data})
    sc = SheetsClient(client=client)

    rows = await sc.read_simple_x4_rows("sheet1", "simple x4")
    assert rows == [], "partial row should be skipped"


@pytest.mark.asyncio
async def test_defaults_applied_when_voice_over_and_zapcap_blank() -> None:
    """Blank VO -> True, blank ZapCap -> False (matches image_vo defaults)."""
    blank = _row(voice_over="", zapcap="")
    data = [_PREVIEW_HEADER, _COLUMN_HEADER, blank]
    client, _ = _make_fake_client({("sheet1", "simple x4"): data})
    sc = SheetsClient(client=client)

    rows = await sc.read_simple_x4_rows("sheet1", "simple x4")
    assert rows[0].voice_over is True
    assert rows[0].zapcap is False


@pytest.mark.asyncio
async def test_per_video_template_cta_pairs_parsed_in_order() -> None:
    """Template1 -> cards[0], Template2 -> cards[1], etc. — order is load-bearing."""
    data = [
        _PREVIEW_HEADER,
        _COLUMN_HEADER,
        _row(
            template_1="1", cta_1="Buy",
            template_2="2", cta_2="Learn",
            template_3="",  cta_3="",
            template_4="1", cta_4="Click",
        ),
    ]
    client, _ = _make_fake_client({("sheet1", "simple x4"): data})
    sc = SheetsClient(client=client)

    rows = await sc.read_simple_x4_rows("sheet1", "simple x4")
    assert [c.template_id for c in rows[0].cards] == ["1", "2", "", "1"]
    assert [c.cta for c in rows[0].cards] == ["Buy", "Learn", "", "Click"]


# ── batch_write_video_urls (writes ready video cols at the shifted positions) ──


@pytest.mark.asyncio
async def test_batch_write_uses_shifted_ready_video_columns() -> None:
    """For simple_x4, Ready Video 1 lives at col R (1-indexed col 18) not J."""
    client, worksheets = _make_fake_client({})
    sc = SheetsClient(client=client)

    writes = [
        PendingWrite(
            job_id="job-test",
            sheet_id="sheet1",
            worksheet="simple x4",
            tab_type=TAB_SIMPLE_X4,
            row_num=5,
            video_urls=[
                "https://example.com/v1.mp4",
                "https://example.com/v2.mp4",
                "",     # blank slot — must NOT clobber the cell
                "https://example.com/v4.mp4",
            ],
            status="done",
            error=None,
        )
    ]

    total = await sc.batch_write_video_urls(writes)
    assert total == 3, "3 non-empty URLs => 3 cell updates (the blank slot is skipped)"

    ws = worksheets[("sheet1", "simple x4")]
    ws.batch_update.assert_called_once()
    (calls,), _ = ws.batch_update.call_args
    written_cells = sorted(u["range"] for u in calls)
    # Ready Video 1..4 live at sheet cols R, S, T, U (1-indexed 18..21) on row 5.
    assert written_cells == ["R5", "S5", "U5"], (
        f"expected writes at R5/S5/U5 (skip T5 blank), got {written_cells}"
    )


@pytest.mark.asyncio
async def test_read_processed_row_nums_skips_two_header_rows() -> None:
    """A simple_x4 row with Ready Video 1 filled at sheet row 3 is "processed".
    Header rows (1, 2) are never reported as processed even if they happen to
    contain text in that column position."""
    done_row = _row(ready_video_1="https://example.com/done.mp4")
    pending_row = _row()

    # Note we intentionally put garbage in the preview header row at the same
    # column position to prove the reader truly skips row 1 + 2.
    bogus_header = _PREVIEW_HEADER + [""] * 30
    bogus_header[SIMPLE_X4_COLS.ready_video_start] = "https://noise.example.com"

    data = [
        bogus_header,
        _COLUMN_HEADER,
        done_row,        # row 3
        pending_row,     # row 4
    ]
    client, _ = _make_fake_client({("sheet1", "simple x4"): data})
    sc = SheetsClient(client=client)

    processed = await sc.read_processed_row_nums(
        "sheet1", "simple x4", layout=TAB_SIMPLE_X4
    )
    assert processed == {3}, (
        f"only the done data row (sheet row 3) should be counted as processed, got {processed}"
    )
