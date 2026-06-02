"""Tests for the Sheets adapter.

The gspread client is replaced with a MagicMock fake — no real network.

Covers:
  - read_image_vo_rows: row_num assignment (sheet is 1-indexed; header is row 1)
  - read_image_vo_rows: defaults applied (voice_over=True, zapcap=False, aspect_ratio=9:16)
  - read_image_vo_rows: blank rows skipped
  - read_image_vo_rows: partial rows (article without seed image) skipped
  - read_four_images_rows: how_many drives image URL selection
  - read_four_images_rows: row with fewer URLs than how_many is skipped
  - batch_write_video_urls groups by (sheet, worksheet) and uses correct columns:
      * Image-VO writes to J,K,L,M (1-indexed cols 10..13)
      * 4Images writes to N,O,P,Q (1-indexed cols 14..17)
  - batch_write_video_urls is a no-op when empty
  - empty video_urls are skipped (no clobbering with blanks)
  - Constructor rejects empty credentials when no client injected
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from bulkvid.adapters.sheets import FOUR_IMAGES_COLS, IMAGE_VO_COLS, SheetsClient
from bulkvid.models.row import FourImagesVO2Row, ImageVORow
from bulkvid.orchestrator.queue import TAB_FOUR_IMAGES, TAB_IMAGE_VO
from bulkvid.orchestrator.sheet_writer import PendingWrite


# ── Fixtures ────────────────────────────────────────────────────────────────


def _make_fake_client(
    sheet_data: dict[tuple[str, str], list[list[str]]],
) -> tuple[MagicMock, dict[tuple[str, str], MagicMock]]:
    """Build a fake gspread client that returns canned data per (sheet, worksheet)."""
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


# ── Constructor ─────────────────────────────────────────────────────────────


def test_constructor_rejects_empty_credentials_without_client() -> None:
    with pytest.raises(ValueError):
        SheetsClient(credentials_file="")


# ── read_image_vo_rows ──────────────────────────────────────────────────────


async def test_read_image_vo_parses_row_with_defaults() -> None:
    sheet_data = {
        ("sheet-A", "Image-VO"): [
            # Header row (ignored).
            ["Country", "Vertical", "Article", "Manual Image",
             "Voice Over", "ZapCap", "Change Size",
             "Script Pattern", "Open Comments",
             "Ready Video 1", "Ready Video 2", "Ready Video 3", "Ready Video 4"],
            # Data row 1 (sheet row 2).
            ["US", "tech",
             "https://example.com/a", "https://example.com/seed.png",
             "", "", "",        # defaults: VO=Yes, ZapCap=No, ratio=9:16
             "How To", "urgent",
             "", "", "", ""],
        ]
    }
    client, _ = _make_fake_client(sheet_data)
    sc = SheetsClient(client=client)

    rows = await sc.read_image_vo_rows("sheet-A", "Image-VO")
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, ImageVORow)
    assert r.row_num == 2                  # sheet rows are 1-indexed; header at row 1
    assert r.country == "US"
    assert r.vertical == "tech"
    assert r.article_url == "https://example.com/a"
    assert r.manual_image_url == "https://example.com/seed.png"
    assert r.voice_over is True            # default
    assert r.zapcap is False               # default
    assert r.aspect_ratio == "9:16"        # default
    assert r.script_pattern == "How To"
    assert r.open_comments == "urgent"


async def test_read_image_vo_honors_explicit_yes_no_values() -> None:
    sheet_data = {
        ("sheet-A", "Image-VO"): [
            ["h"] * 13,
            ["US", "tech",
             "https://a/", "https://s/",
             "No", "Yes", "1:1",
             "", "",
             "", "", "", ""],
        ]
    }
    client, _ = _make_fake_client(sheet_data)
    sc = SheetsClient(client=client)
    rows = await sc.read_image_vo_rows("sheet-A", "Image-VO")
    assert rows[0].voice_over is False
    assert rows[0].zapcap is True
    assert rows[0].aspect_ratio == "1:1"


async def test_read_image_vo_skips_blank_and_partial_rows() -> None:
    sheet_data = {
        ("sheet-A", "Image-VO"): [
            ["h"] * 13,
            ["", "", "", "", "", "", "", "", "", "", "", "", ""],        # blank -> skip
            ["US", "tech", "https://a/", "", "", "", "", "", "", "", "", "", ""],  # missing seed -> skip
            ["US", "tech", "https://a/", "https://s/", "", "", "", "", "", "", "", "", ""],  # ok
        ]
    }
    client, _ = _make_fake_client(sheet_data)
    sc = SheetsClient(client=client)
    rows = await sc.read_image_vo_rows("sheet-A", "Image-VO")
    assert len(rows) == 1
    # The valid row is at sheet row 4 (header + 2 skipped + this one).
    assert rows[0].row_num == 4


async def test_read_image_vo_empty_sheet() -> None:
    sheet_data: dict[tuple[str, str], list[list[str]]] = {("sheet-A", "Image-VO"): []}
    client, _ = _make_fake_client(sheet_data)
    sc = SheetsClient(client=client)
    rows = await sc.read_image_vo_rows("sheet-A", "Image-VO")
    assert rows == []


# ── read_four_images_rows ───────────────────────────────────────────────────


async def test_read_four_images_uses_first_n_urls() -> None:
    sheet_data = {
        ("sheet-B", "4Images-VO2"): [
            ["h"] * 17,
            # Country, Vertical, Article, How Many, VO, I1, I2, I3, I4, ZapCap, Aspect, Pattern, Open, then 4 outputs
            ["US", "tech", "https://art/", "2", "",
             "https://i1/", "https://i2/", "https://i3/", "https://i4/",
             "", "9:16", "", "",
             "", "", "", ""],
        ]
    }
    client, _ = _make_fake_client(sheet_data)
    sc = SheetsClient(client=client)
    rows = await sc.read_four_images_rows("sheet-B", "4Images-VO2")
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, FourImagesVO2Row)
    assert r.how_many == 2
    assert r.image_urls == ["https://i1/", "https://i2/"]


async def test_read_four_images_skips_row_when_urls_missing() -> None:
    sheet_data = {
        ("sheet-B", "4Images-VO2"): [
            ["h"] * 17,
            # how_many=3 but only 2 URLs supplied -> skip
            ["US", "tech", "https://art/", "3", "",
             "https://i1/", "https://i2/", "", "",
             "", "9:16", "", "",
             "", "", "", ""],
        ]
    }
    client, _ = _make_fake_client(sheet_data)
    sc = SheetsClient(client=client)
    rows = await sc.read_four_images_rows("sheet-B", "4Images-VO2")
    assert rows == []


async def test_read_four_images_skips_invalid_how_many() -> None:
    sheet_data = {
        ("sheet-B", "4Images-VO2"): [
            ["h"] * 17,
            ["US", "tech", "https://art/", "5", "",
             "https://i1/", "https://i2/", "https://i3/", "https://i4/",
             "", "9:16", "", "",
             "", "", "", ""],
        ]
    }
    client, _ = _make_fake_client(sheet_data)
    sc = SheetsClient(client=client)
    rows = await sc.read_four_images_rows("sheet-B", "4Images-VO2")
    assert rows == []


# ── batch_write_video_urls ─────────────────────────────────────────────────


def _write(
    *,
    job_id: str = "job-1",
    sheet_id: str = "sheet-A",
    worksheet: str = "Image-VO",
    tab_type: str = TAB_IMAGE_VO,
    row_num: int,
    video_urls: list[str],
) -> PendingWrite:
    return PendingWrite(
        job_id=job_id, sheet_id=sheet_id, worksheet=worksheet, tab_type=tab_type,
        row_num=row_num, video_urls=video_urls, status="SUCCESS", error=None,
    )


async def test_batch_write_empty_returns_zero() -> None:
    client, _ = _make_fake_client({})
    sc = SheetsClient(client=client)
    n = await sc.batch_write_video_urls([])
    assert n == 0


async def test_image_vo_writes_to_columns_J_through_M() -> None:
    client, worksheets = _make_fake_client({})
    sc = SheetsClient(client=client)

    n = await sc.batch_write_video_urls(
        [
            _write(
                row_num=2,
                video_urls=["u1", "u2", "u3", "u4"],
            )
        ]
    )
    assert n == 4

    ws = worksheets[("sheet-A", "Image-VO")]
    ws.batch_update.assert_called_once()
    updates = ws.batch_update.call_args.args[0]
    cells = {u["range"]: u["values"][0][0] for u in updates}
    # Image-VO: ready videos start at col J (1-indexed col 10), 0-indexed col 9.
    assert cells == {"J2": "u1", "K2": "u2", "L2": "u3", "M2": "u4"}


async def test_four_images_writes_to_columns_N_through_Q() -> None:
    client, worksheets = _make_fake_client({})
    sc = SheetsClient(client=client)

    await sc.batch_write_video_urls(
        [
            _write(
                sheet_id="sheet-B",
                worksheet="4Images-VO2",
                tab_type=TAB_FOUR_IMAGES,
                row_num=3,
                video_urls=["u1", "u2"],   # how_many=2 row → only 2 outputs
            )
        ]
    )

    ws = worksheets[("sheet-B", "4Images-VO2")]
    updates = ws.batch_update.call_args.args[0]
    cells = {u["range"]: u["values"][0][0] for u in updates}
    # 4Images: ready videos start at col N (1-indexed col 14), 0-indexed col 13.
    assert cells == {"N3": "u1", "O3": "u2"}


async def test_groups_by_sheet_and_worksheet_one_batch_each() -> None:
    client, worksheets = _make_fake_client({})
    sc = SheetsClient(client=client)

    await sc.batch_write_video_urls(
        [
            _write(sheet_id="sheet-A", worksheet="Image-VO", row_num=2, video_urls=["a"]),
            _write(sheet_id="sheet-A", worksheet="Image-VO", row_num=3, video_urls=["b"]),
            _write(sheet_id="sheet-B", worksheet="Image-VO", row_num=2, video_urls=["c"]),
        ]
    )

    ws_a = worksheets[("sheet-A", "Image-VO")]
    ws_b = worksheets[("sheet-B", "Image-VO")]
    # Sheet A got one batch with 2 cells (J2, J3); sheet B got one with 1 cell.
    assert ws_a.batch_update.call_count == 1
    assert ws_b.batch_update.call_count == 1
    assert len(ws_a.batch_update.call_args.args[0]) == 2
    assert len(ws_b.batch_update.call_args.args[0]) == 1


async def test_skips_empty_url_slots() -> None:
    client, worksheets = _make_fake_client({})
    sc = SheetsClient(client=client)

    n = await sc.batch_write_video_urls(
        [_write(row_num=2, video_urls=["u1", "", "u3", ""])]
    )

    # Empty strings shouldn't overwrite existing cells with blanks.
    ws = worksheets[("sheet-A", "Image-VO")]
    updates = ws.batch_update.call_args.args[0]
    cells = {u["range"]: u["values"][0][0] for u in updates}
    assert cells == {"J2": "u1", "L2": "u3"}
    assert n == 2


# ── Column-map sanity ───────────────────────────────────────────────────────


def test_column_constants_match_plan() -> None:
    # Plan §15 Appendix A: Image-VO ready videos start at column J (1-indexed col 10
    # = 0-indexed col 9). 4Images at column N (col 14 = 0-indexed 13).
    assert IMAGE_VO_COLS.ready_video_start == 9
    assert FOUR_IMAGES_COLS.ready_video_start == 13
