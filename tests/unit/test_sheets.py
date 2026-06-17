"""Tests for the Sheets adapter.

The gspread client is replaced with a MagicMock fake — no real network.

Covers:
  - read_image_vo_rows: row_num assignment (sheet is 1-indexed; header is row 1)
  - read_image_vo_rows: defaults applied (voice_over=True, zapcap=False);
    aspect_ratio passes through blank ("") so the row processor's
    ``resolve_aspect_ratio`` can probe the manual image's native dimensions
    (plan _plans/2026-06-14-blank-size-uses-native-image.md)
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

from unittest.mock import MagicMock

import pytest

from bulkvid.adapters.sheets import FOUR_IMAGES_COLS, IMAGE_VO_COLS, SheetsClient
from bulkvid.models.row import CartoonRow, FourImagesVO2Row, ImageVORow
from bulkvid.orchestrator.queue import (
    TAB_AVATAR,
    TAB_FOUR_IMAGES,
    TAB_IMAGE_VO,
    TAB_YT_CARTOON,
)
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


def test_constructor_rejects_all_empty() -> None:
    with pytest.raises(ValueError, match="credentials_file|credentials_info|client"):
        SheetsClient(credentials_file="", credentials_info=None)


def test_constructor_accepts_credentials_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """The inline-env-var path: pass a dict, get a working SheetsClient.

    We stub ``Credentials.from_service_account_info`` + ``gspread.authorize``
    so the test doesn't need a real RSA key. The point of the test is wiring,
    not crypto.
    """
    import bulkvid.adapters.sheets as sheets_mod

    captured: dict = {}

    def _fake_from_info(info, scopes):    # noqa: ANN001
        captured["info"] = info
        captured["scopes"] = scopes
        return "fake-creds"

    def _fake_authorize(creds):    # noqa: ANN001
        captured["creds"] = creds
        return MagicMock(name="fake-gspread-client")

    monkeypatch.setattr(
        sheets_mod.Credentials, "from_service_account_info", _fake_from_info
    )
    monkeypatch.setattr(sheets_mod.gspread, "authorize", _fake_authorize)

    info = {"type": "service_account", "client_email": "x@y", "private_key": "fake"}
    sc = SheetsClient(credentials_info=info)
    assert sc._client is not None
    assert captured["info"] is info
    assert captured["scopes"] == list(sheets_mod.GSHEETS_SCOPES)


# ── build_client_from_settings ──────────────────────────────────────────────


def test_build_client_from_settings_uses_file_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """File path wins over inline env vars when both are configured."""
    from bulkvid.adapters import sheets as sheets_mod
    from bulkvid.config import Settings

    sa = tmp_path / "sa.json"
    sa.write_text("{}")
    settings = Settings(
        SHEETS_SERVICE_ACCOUNT_FILE=str(sa),
        GOOGLE_PRIVATE_KEY="should-be-ignored",
        GOOGLE_CLIENT_EMAIL="should-be-ignored@x",
    )

    captured: dict = {}

    def _fake_from_file(path, scopes):    # noqa: ANN001
        captured["path"] = path
        return "creds-from-file"

    def _fake_authorize(creds):    # noqa: ANN001
        return MagicMock()

    monkeypatch.setattr(
        sheets_mod.Credentials, "from_service_account_file", _fake_from_file
    )
    monkeypatch.setattr(sheets_mod.gspread, "authorize", _fake_authorize)

    sc = sheets_mod.build_client_from_settings(settings)
    assert sc is not None
    assert captured["path"] == str(sa)


def test_build_client_from_settings_falls_back_to_inline_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When SHEETS_SERVICE_ACCOUNT_FILE is empty but GOOGLE_* vars are set,
    the builder uses the inline-env-var path (the PA-style setup)."""
    from bulkvid.adapters import sheets as sheets_mod
    from bulkvid.config import Settings

    settings = Settings(
        SHEETS_SERVICE_ACCOUNT_FILE="",
        GOOGLE_PROJECT_ID="proj",
        GOOGLE_CLIENT_EMAIL="bot@proj.iam.gserviceaccount.com",
        GOOGLE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
        GOOGLE_CLIENT_ID="42",
    )

    captured: dict = {}

    def _fake_from_info(info, scopes):    # noqa: ANN001
        captured["info"] = info
        return "creds-from-info"

    def _fake_authorize(creds):    # noqa: ANN001
        return MagicMock()

    monkeypatch.setattr(
        sheets_mod.Credentials, "from_service_account_info", _fake_from_info
    )
    monkeypatch.setattr(sheets_mod.gspread, "authorize", _fake_authorize)

    sc = sheets_mod.build_client_from_settings(settings)
    assert sc is not None
    # The dict that reached the constructor should have the right fields.
    assert captured["info"]["client_email"] == "bot@proj.iam.gserviceaccount.com"
    assert captured["info"]["project_id"] == "proj"


def test_build_client_from_settings_raises_when_neither_configured() -> None:
    from bulkvid.adapters import sheets as sheets_mod
    from bulkvid.config import Settings

    settings = Settings(SHEETS_SERVICE_ACCOUNT_FILE="")
    with pytest.raises(ValueError, match="not configured"):
        sheets_mod.build_client_from_settings(settings)


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
             "", "", "",        # defaults: VO=Yes, ZapCap=No, ratio="" (probed at row time)
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
    # Blank "Change Size" flows through verbatim — the row processor's
    # ``resolve_aspect_ratio`` translates it into the manual image's
    # native pixel dimensions ("WxH") at processor entry. Plan
    # ``_plans/2026-06-14-blank-size-uses-native-image.md``.
    assert r.aspect_ratio == ""
    assert r.script_pattern == "How To"
    assert r.open_comments == "urgent"


async def test_read_cartoon_ignores_manual_image_and_needs_only_article() -> None:
    sheet_data = {
        ("sheet-C", "cartoon"): [
            ["Country", "Vertical", "Article", "Manual Image",
             "Voice Over", "ZapCap", "Change Size",
             "Script Pattern", "Open Comments",
             "Ready Video 1", "Ready Video 2"],
            # Manual Image column is blank — cartoon doesn't need it.
            ["MX", "automotive", "https://example.com/a", "",
             "", "", "9:16", "How To", "", "", ""],
        ]
    }
    client, _ = _make_fake_client(sheet_data)
    sc = SheetsClient(client=client)

    rows = await sc.read_cartoon_rows("sheet-C", "cartoon")
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, CartoonRow)
    assert r.row_num == 2
    assert r.article_url == "https://example.com/a"
    assert r.voice_over is True            # default
    assert r.aspect_ratio == "9:16"
    assert not hasattr(r, "manual_image_url")


async def test_read_cartoon_skips_rows_without_article() -> None:
    sheet_data = {
        ("sheet-C", "cartoon"): [
            ["h"] * 11,
            ["MX", "automotive", "", "", "", "", "9:16", "", "", "", ""],
        ]
    }
    client, _ = _make_fake_client(sheet_data)
    sc = SheetsClient(client=client)
    rows = await sc.read_cartoon_rows("sheet-C", "cartoon")
    assert rows == []


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


async def test_yt_cartoon_writes_to_columns_P_and_Q() -> None:
    """Regression for the dead-write bug (Yoav 2026-06-17): the yt-cartoon
    tab had no branch in ``batch_write_video_urls``'s positional_fallback
    chain, so it hit ``else None`` and the write was SKIPPED entirely — the
    finished videos never landed in P/Q. With no header row in the fake
    client, the write MUST fall back to the positional column (P = 0-indexed
    15) and write BOTH ready videos."""
    client, worksheets = _make_fake_client({})
    sc = SheetsClient(client=client)

    n = await sc.batch_write_video_urls(
        [
            _write(
                sheet_id="sheet-Y",
                worksheet="yt-cartoon",
                tab_type=TAB_YT_CARTOON,
                row_num=2,
                video_urls=["u1", "u2"],   # 10s bucket → 2 videos
            )
        ]
    )
    assert n == 2

    ws = worksheets[("sheet-Y", "yt-cartoon")]
    updates = ws.batch_update.call_args.args[0]
    cells = {u["range"]: u["values"][0][0] for u in updates}
    # yt-cartoon: ready videos start at col P (1-indexed col 16), 0-indexed 15.
    assert cells == {"P2": "u1", "Q2": "u2"}


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


async def test_avatar_writes_to_ready_video_header_column_when_shifted() -> None:
    """Regression for chat 2026-06-09: the operator added Avatar Size +
    Avatar Shape columns at F + G, shifting every column right by 2.
    The hardcoded ``AVATAR_COLS.ready_video_start = 12`` (column M) was
    no longer where "Ready Video" actually lived (now column O), so the
    URL landed in CTA Text instead.

    With header-based resolution, the writer looks up the column whose
    row-1 header reads ``Ready Video`` and writes there — column O on
    this shifted sheet.
    """
    client, worksheets = _make_fake_client({})
    sc = SheetsClient(client=client)
    # Pre-create the worksheet mock so we can set its row_values BEFORE
    # the batch_write touches it. row 1 has the shifted layout the
    # operator's actual sheet showed in the screenshot.
    ws = client.open_by_key("sheet-A").worksheet("avatar tab")
    ws.row_values = MagicMock(return_value=[
        "Country", "Vertical", "Article", "Manual Image", "Avatar ID (NEW)",
        "Avatar Size", "Avatar Shape",
        "Voice Over", "ZapCap", "Change Size", "Script Pattern",
        "CTA", "CTA Text", "Open Comments",
        "Ready Video",
    ])

    await sc.batch_write_video_urls([
        _write(
            sheet_id="sheet-A",
            worksheet="avatar tab",
            tab_type=TAB_AVATAR,
            row_num=2,
            video_urls=["https://video.test/avatar.mp4"],
        )
    ])

    updates = ws.batch_update.call_args.args[0]
    cells = {u["range"]: u["values"][0][0] for u in updates}
    # Header says Ready Video sits at column O (15th column, 1-indexed)
    # — that's where the URL must land, NOT M (the old AVATAR_COLS
    # positional fallback).
    assert cells == {"O2": "https://video.test/avatar.mp4"}, (
        f"expected URL at O2 (Ready Video header position), got {cells!r}"
    )


async def test_avatar_writes_falls_back_to_positional_when_no_header() -> None:
    """Brand-new sheet with no row-1 headers: the writer must keep using
    today's positional fallback (column M for the avatar tab) so existing
    sheets without headers don't regress."""
    client, worksheets = _make_fake_client({})
    sc = SheetsClient(client=client)
    # row_values returns empty -> header lookup fails -> positional fallback.
    ws = client.open_by_key("sheet-A").worksheet("avatar tab")
    ws.row_values = MagicMock(return_value=[])

    await sc.batch_write_video_urls([
        _write(
            sheet_id="sheet-A",
            worksheet="avatar tab",
            tab_type=TAB_AVATAR,
            row_num=2,
            video_urls=["https://video.test/fallback.mp4"],
        )
    ])

    updates = ws.batch_update.call_args.args[0]
    cells = {u["range"]: u["values"][0][0] for u in updates}
    # AVATAR_COLS.ready_video_start = 12 (0-indexed) → column M.
    assert cells == {"M2": "https://video.test/fallback.mp4"}


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


# ── read_header_row ─────────────────────────────────────────────────────────


async def test_read_header_row_returns_row_1() -> None:
    # Custom client because the standard fake only mocks get_all_values.
    ws = MagicMock()
    ws.row_values = MagicMock(return_value=["Country", "Vertical", "Article", "Manual Image"])
    spreadsheet = MagicMock()
    spreadsheet.worksheet = MagicMock(return_value=ws)
    client = MagicMock()
    client.open_by_key = MagicMock(return_value=spreadsheet)

    sc = SheetsClient(client=client)
    headers = await sc.read_header_row("sheet-A", "Image-VO")
    assert headers == ["Country", "Vertical", "Article", "Manual Image"]
    ws.row_values.assert_called_once_with(1)


# ── list_worksheets ─────────────────────────────────────────────────────────


async def test_list_worksheets_returns_tab_titles_in_order() -> None:
    ws_a, ws_b, ws_c = MagicMock(), MagicMock(), MagicMock()
    ws_a.title = "image_vo"
    ws_b.title = "simple"
    ws_c.title = "cartoon"
    spreadsheet = MagicMock()
    spreadsheet.worksheets = MagicMock(return_value=[ws_a, ws_b, ws_c])
    client = MagicMock()
    client.open_by_key = MagicMock(return_value=spreadsheet)

    sc = SheetsClient(client=client)
    names = await sc.list_worksheets("sheet-A")
    assert names == ["image_vo", "simple", "cartoon"]


# ── read_processed_row_nums ─────────────────────────────────────────────────


async def test_read_processed_row_nums_image_vo_layout() -> None:
    # Image-VO: ready_video_start = 0-indexed col 9 (column J).
    # Row 2 (J empty) is unprocessed; row 3 (J set) is processed.
    sheet_data = {
        ("sheet-A", "Image-VO"): [
            # Header (ignored).
            ["", "", "", "", "", "", "", "", "", "Ready Video 1"],
            # Row 2: J empty -> unprocessed.
            ["US", "news", "http://a", "http://img", "Yes", "No", "9:16", "", "", ""],
            # Row 3: J set -> processed.
            ["US", "news", "http://a", "http://img", "Yes", "No", "9:16", "", "", "http://v1"],
            # Row 4: J empty -> unprocessed.
            ["US", "news", "http://a", "http://img", "Yes", "No", "9:16", "", "", ""],
            # Row 5: J set with whitespace only -> still unprocessed.
            ["US", "news", "http://a", "http://img", "Yes", "No", "9:16", "", "", "   "],
        ],
    }
    client, _ = _make_fake_client(sheet_data)
    sc = SheetsClient(client=client)
    processed = await sc.read_processed_row_nums(
        "sheet-A", "Image-VO", layout=TAB_IMAGE_VO
    )
    assert processed == {3}


async def test_read_processed_row_nums_four_images_layout() -> None:
    # 4Images: ready_video_start = 0-indexed col 13 (column N).
    sheet_data = {
        ("sheet-B", "4Images"): [
            # Header padded out to col N.
            [""] * 13 + ["Ready Video 1"],
            # Row 2: N empty -> unprocessed.
            [""] * 13 + [""],
            # Row 3: N set -> processed.
            [""] * 13 + ["http://v1"],
        ],
    }
    client, _ = _make_fake_client(sheet_data)
    sc = SheetsClient(client=client)
    processed = await sc.read_processed_row_nums(
        "sheet-B", "4Images", layout=TAB_FOUR_IMAGES
    )
    assert processed == {3}


async def test_read_processed_row_nums_unknown_layout_returns_empty() -> None:
    client, _ = _make_fake_client({})
    sc = SheetsClient(client=client)
    processed = await sc.read_processed_row_nums(
        "sheet-A", "Foo", layout="bogus"
    )
    assert processed == set()


# ── Retry behavior ──────────────────────────────────────────────────────────


class _FakeAPIError(Exception):
    """Stand-in shape for gspread.exceptions.APIError carrying a status_code."""

    def __init__(self, status_code: int, message: str = "fake") -> None:
        super().__init__(message)
        self.response = MagicMock()
        self.response.status_code = status_code


def _make_flaky_read_client(
    failures_before_success: int, sheet_data: list[list[str]]
) -> tuple[MagicMock, MagicMock]:
    """Build a client whose ``get_all_values`` raises 429 N times then returns data."""
    ws = MagicMock()
    call_log: list[int] = []

    def _side_effect() -> list[list[str]]:
        call_log.append(1)
        if len(call_log) <= failures_before_success:
            raise _FakeAPIError(429, "rate limited")
        return sheet_data

    ws.get_all_values = MagicMock(side_effect=_side_effect)
    spreadsheet = MagicMock()
    spreadsheet.worksheet = MagicMock(return_value=ws)
    client = MagicMock()
    client.open_by_key = MagicMock(return_value=spreadsheet)
    return client, ws


@pytest.fixture
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    from bulkvid.adapters import _retry

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_retry.asyncio, "sleep", _instant)


async def test_read_retries_on_429_then_succeeds(_no_sleep: None) -> None:
    # One header row + one data row Image-VO shape (need article + manual image).
    data = [
        ["country", "vertical", "article", "manual_image", "voice_over", "zapcap",
         "aspect", "pattern", "comments"],
        ["US", "fashion", "http://a", "http://i", "Yes", "No",
         "9:16", "How To", ""],
    ]
    client, ws = _make_flaky_read_client(failures_before_success=2, sheet_data=data)
    sc = SheetsClient(client=client)

    rows = await sc.read_image_vo_rows("sheet-A", "Image-VO")
    assert len(rows) == 1
    assert ws.get_all_values.call_count == 3


async def test_read_exhausts_then_raises_rate_limit(_no_sleep: None) -> None:
    from bulkvid.adapters.sheets import SheetsRateLimitError

    client, ws = _make_flaky_read_client(failures_before_success=10, sheet_data=[])
    sc = SheetsClient(client=client)

    with pytest.raises(SheetsRateLimitError):
        await sc.read_image_vo_rows("sheet-A", "Image-VO")

    # Default attempts=3 for reads.
    assert ws.get_all_values.call_count == 3


async def test_write_retries_at_most_once(_no_sleep: None) -> None:
    """Writes are NOT idempotent — cap retries at 1 to avoid double-writes."""
    from bulkvid.adapters.sheets import SheetsRateLimitError

    ws = MagicMock()
    ws.batch_update = MagicMock(side_effect=_FakeAPIError(429))
    spreadsheet = MagicMock()
    spreadsheet.worksheet = MagicMock(return_value=ws)
    client = MagicMock()
    client.open_by_key = MagicMock(return_value=spreadsheet)

    sc = SheetsClient(client=client)
    write = PendingWrite(
        job_id="job-1",
        sheet_id="sheet-A",
        worksheet="Image-VO",
        tab_type=TAB_IMAGE_VO,
        row_num=2,
        video_urls=["http://v1.mp4"],
        status="done",
        error="",
    )
    with pytest.raises(SheetsRateLimitError):
        await sc.batch_write_video_urls([write])

    # attempts=2 → 1 original + 1 retry = 2 calls.
    assert ws.batch_update.call_count == 2


async def test_read_does_not_retry_on_unknown_error(_no_sleep: None) -> None:
    # An unrelated exception (ValueError) is left alone by the classifier; the
    # read fails on attempt 1.
    ws = MagicMock()
    ws.get_all_values = MagicMock(side_effect=ValueError("you messed up"))
    spreadsheet = MagicMock()
    spreadsheet.worksheet = MagicMock(return_value=ws)
    client = MagicMock()
    client.open_by_key = MagicMock(return_value=spreadsheet)

    sc = SheetsClient(client=client)
    with pytest.raises(ValueError):
        await sc.read_image_vo_rows("sheet-A", "Image-VO")

    assert ws.get_all_values.call_count == 1
