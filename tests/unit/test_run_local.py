"""Tests for ``tools/run_local.py`` — the standalone bulk-video runner.

Scope is deliberately narrow: only what is new to the local runner.
  - Row-range parsing (CLI input → sheet row numbers)
  - Tab → reader / dispatch wiring
  - PendingWrite construction
  - The concurrency loop (with stubbed processors), incl. concurrency cap,
    immediate write-back per row, exception → INTERNAL_ERROR mapping
  - Prereq validation
  - Argv parsing

We do NOT re-test row processors, the runner, the sheet adapter, or any
pipeline module — those have their own dedicated suites.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# ``tools/`` is not part of the installed wheel — make it importable from
# the project root so the runner script can be unit-tested.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from bulkvid.config import Settings    # noqa: E402
from bulkvid.models.row import (    # noqa: E402
    STATUS_ARTICLE_FETCH_FAILED,
    STATUS_INTERNAL_ERROR,
    STATUS_SUCCESS,
    CartoonRow,
    FourImagesVO2Row,
    ImageVORow,
    RowResult,
    SimpleRow,
)
from bulkvid.orchestrator.queue import (    # noqa: E402
    TAB_CARTOON,
    TAB_FOUR_IMAGES,
    TAB_IMAGE_VO,
    TAB_SIMPLE,
)
from tools import run_local    # noqa: E402


# ── parse_row_range ─────────────────────────────────────────────────────────


def test_parse_row_range_single() -> None:
    assert run_local.parse_row_range("5") == [5]


def test_parse_row_range_comma_list() -> None:
    assert run_local.parse_row_range("5,7,12") == [5, 7, 12]


def test_parse_row_range_range() -> None:
    assert run_local.parse_row_range("5-9") == [5, 6, 7, 8, 9]


def test_parse_row_range_mixed() -> None:
    assert run_local.parse_row_range("5,7,9-12") == [5, 7, 9, 10, 11, 12]


def test_parse_row_range_dedup_and_sort() -> None:
    # Out-of-order + overlapping range should land sorted + deduped.
    assert run_local.parse_row_range("12,5,8-10,9") == [5, 8, 9, 10, 12]


def test_parse_row_range_whitespace_tolerated() -> None:
    assert run_local.parse_row_range(" 5 , 7-9 ") == [5, 7, 8, 9]


def test_parse_row_range_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        run_local.parse_row_range("")
    with pytest.raises(ValueError, match="empty"):
        run_local.parse_row_range("   ")


def test_parse_row_range_rejects_zero() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        run_local.parse_row_range("0")


def test_parse_row_range_rejects_negative() -> None:
    # "-5" splits to ["", "5"] which is not a valid range token.
    with pytest.raises(ValueError):
        run_local.parse_row_range("-5")


def test_parse_row_range_rejects_reversed_range() -> None:
    with pytest.raises(ValueError, match="reversed"):
        run_local.parse_row_range("9-5")


def test_parse_row_range_rejects_non_numeric() -> None:
    with pytest.raises(ValueError, match="not numeric"):
        run_local.parse_row_range("5,abc")


def test_parse_row_range_names_bad_token() -> None:
    with pytest.raises(ValueError, match="abc"):
        run_local.parse_row_range("5,abc,7")


# ── process_one_row dispatch ────────────────────────────────────────────────


def _image_vo_row() -> ImageVORow:
    return ImageVORow(
        row_num=5, country="US", vertical="news", article_url="http://a",
        manual_image_url="http://i", voice_over=True, zapcap=False,
        aspect_ratio="9:16", script_pattern="", open_comments="",
    )


def _simple_row() -> SimpleRow:
    return SimpleRow(
        row_num=7, country="US", vertical="news", article_url="http://a",
        manual_image_url="http://i", voice_over=True, zapcap=False,
        aspect_ratio="9:16", script_pattern="", open_comments="",
    )


def _four_images_row() -> FourImagesVO2Row:
    return FourImagesVO2Row(
        row_num=9, country="US", vertical="news", article_url="http://a",
        how_many=2, voice_over=True, image_urls=["http://i1", "http://i2"],
        zapcap=False, aspect_ratio="9:16", script_pattern="", open_comments="",
    )


def _cartoon_row() -> CartoonRow:
    return CartoonRow(
        row_num=11, country="US", vertical="news", article_url="http://a",
        voice_over=True, zapcap=False, aspect_ratio="9:16",
        script_pattern="", open_comments="",
    )


@pytest.mark.parametrize(
    "tab, row_factory, target_fn",
    [
        (TAB_IMAGE_VO, _image_vo_row, "process_image_vo_row"),
        (TAB_FOUR_IMAGES, _four_images_row, "process_4images_vo2_row"),
        (TAB_SIMPLE, _simple_row, "process_simple_row"),
        (TAB_CARTOON, _cartoon_row, "process_cartoon_row"),
    ],
)
async def test_process_one_row_dispatches_by_tab(
    monkeypatch: pytest.MonkeyPatch, tab: str, row_factory, target_fn: str
) -> None:
    called: list[str] = []

    async def _fake(row, clients, *, job_id):    # noqa: ARG001
        called.append(target_fn)
        return RowResult(row_num=row.row_num, status=STATUS_SUCCESS)

    monkeypatch.setattr(run_local, target_fn, _fake)

    result = await run_local.process_one_row(
        tab, row_factory(), clients=None, job_id="job-1"    # type: ignore[arg-type]
    )
    assert called == [target_fn]
    assert result.status == STATUS_SUCCESS


async def test_process_one_row_unknown_tab_raises() -> None:
    with pytest.raises(ValueError, match="Unknown tab"):
        await run_local.process_one_row("bogus", _image_vo_row(), clients=None, job_id="x")    # type: ignore[arg-type]


# ── read_rows_for_tab dispatch ──────────────────────────────────────────────


class _FakeSheets:
    """Minimal stand-in for SheetsClient — records which reader was called."""

    def __init__(self) -> None:
        self.called: list[tuple[str, str, str]] = []

    async def read_image_vo_rows(self, sheet_id: str, ws: str) -> list[ImageVORow]:
        self.called.append(("image_vo", sheet_id, ws))
        return [_image_vo_row()]

    async def read_four_images_rows(
        self, sheet_id: str, ws: str
    ) -> list[FourImagesVO2Row]:
        self.called.append(("four_images", sheet_id, ws))
        return [_four_images_row()]

    async def read_cartoon_rows(self, sheet_id: str, ws: str) -> list[CartoonRow]:
        self.called.append(("cartoon", sheet_id, ws))
        return [_cartoon_row()]


async def test_read_rows_image_vo() -> None:
    sheets = _FakeSheets()
    rows = await run_local.read_rows_for_tab(sheets, TAB_IMAGE_VO, "sid", "ws")    # type: ignore[arg-type]
    assert sheets.called == [("image_vo", "sid", "ws")]
    assert len(rows) == 1
    assert isinstance(rows[0], ImageVORow)


async def test_read_rows_four_images() -> None:
    sheets = _FakeSheets()
    rows = await run_local.read_rows_for_tab(sheets, TAB_FOUR_IMAGES, "sid", "ws")    # type: ignore[arg-type]
    assert sheets.called == [("four_images", "sid", "ws")]
    assert isinstance(rows[0], FourImagesVO2Row)


async def test_read_rows_cartoon() -> None:
    sheets = _FakeSheets()
    rows = await run_local.read_rows_for_tab(sheets, TAB_CARTOON, "sid", "ws")    # type: ignore[arg-type]
    assert sheets.called == [("cartoon", "sid", "ws")]
    assert isinstance(rows[0], CartoonRow)


async def test_read_rows_simple_reuses_image_vo_reader_and_converts() -> None:
    # Simple shares the Image-VO column layout; the runner reads via the
    # image_vo reader and converts each row to a SimpleRow.
    sheets = _FakeSheets()
    rows = await run_local.read_rows_for_tab(sheets, TAB_SIMPLE, "sid", "ws")    # type: ignore[arg-type]
    assert sheets.called == [("image_vo", "sid", "ws")]
    assert len(rows) == 1
    assert isinstance(rows[0], SimpleRow)
    # Fields propagated.
    src = _image_vo_row()
    assert rows[0].row_num == src.row_num
    assert rows[0].article_url == src.article_url
    assert rows[0].manual_image_url == src.manual_image_url


async def test_read_rows_unknown_tab_raises() -> None:
    with pytest.raises(ValueError, match="Unknown tab"):
        await run_local.read_rows_for_tab(_FakeSheets(), "bogus", "sid", "ws")    # type: ignore[arg-type]


# ── pending_write_from_result ───────────────────────────────────────────────


def test_pending_write_from_result_carries_all_fields() -> None:
    r = RowResult(
        row_num=7,
        status=STATUS_SUCCESS,
        video_urls=["u1", "u2", "u3", "u4"],
        error=None,
    )
    pw = run_local.pending_write_from_result(
        sheet_id="sid", worksheet="ws", tab=TAB_IMAGE_VO, job_id="job-1", result=r,
    )
    assert pw.sheet_id == "sid"
    assert pw.worksheet == "ws"
    assert pw.tab_type == TAB_IMAGE_VO
    assert pw.job_id == "job-1"
    assert pw.row_num == 7
    assert pw.video_urls == ["u1", "u2", "u3", "u4"]
    assert pw.status == STATUS_SUCCESS
    assert pw.error is None


def test_pending_write_carries_failure_error() -> None:
    r = RowResult(
        row_num=8,
        status=STATUS_ARTICLE_FETCH_FAILED,
        video_urls=[],
        error="timeout after 15s",
    )
    pw = run_local.pending_write_from_result(
        sheet_id="sid", worksheet="ws", tab=TAB_CARTOON, job_id="j", result=r,
    )
    assert pw.video_urls == []
    assert pw.status == STATUS_ARTICLE_FETCH_FAILED
    assert pw.error == "timeout after 15s"


# ── summary_line ────────────────────────────────────────────────────────────


def test_summary_line_success() -> None:
    r = RowResult(
        row_num=5, status=STATUS_SUCCESS, video_urls=["u1", "u2"],
        cost_usd=0.1234, elapsed_seconds=87.4,
    )
    out = run_local.summary_line(r)
    assert "Row 5: SUCCESS" in out
    assert "2 videos" in out
    assert "$0.1234" in out
    assert "87.4s" in out


def test_summary_line_single_video_not_plural() -> None:
    r = RowResult(row_num=5, status=STATUS_SUCCESS, video_urls=["u1"])
    out = run_local.summary_line(r)
    assert "1 video " in out
    assert "1 videos" not in out


def test_summary_line_failure_includes_status_and_error() -> None:
    r = RowResult(
        row_num=7, status=STATUS_ARTICLE_FETCH_FAILED, error="timeout after 15s"
    )
    out = run_local.summary_line(r)
    assert "Row 7: FAILED" in out
    assert STATUS_ARTICLE_FETCH_FAILED in out
    assert "timeout after 15s" in out


def test_summary_line_failure_collapses_newlines() -> None:
    r = RowResult(row_num=7, status="INTERNAL_ERROR", error="line1\nline2")
    out = run_local.summary_line(r)
    assert "line1 line2" in out
    assert "\n" not in out.split("\"", 1)[1].rsplit("\"", 1)[0]


# ── validate_prereqs ────────────────────────────────────────────────────────


def _full_settings(**overrides) -> Settings:
    base = dict(
        OPENAI_API_KEY="sk-test",
        KIE_AI_KEYS="kie_AAAA",
        RENDI_API_KEY="rendi-test",
        SHEETS_SERVICE_ACCOUNT_FILE="",     # caller will set
    )
    base.update(overrides)
    return Settings(**base)


def test_validate_prereqs_all_set(tmp_path: Path) -> None:
    sa = tmp_path / "sa.json"
    sa.write_text("{}")
    settings = _full_settings(SHEETS_SERVICE_ACCOUNT_FILE=str(sa))
    assert run_local.validate_prereqs(settings) == []


def test_validate_prereqs_missing_sheets_credentials() -> None:
    settings = _full_settings()
    errors = run_local.validate_prereqs(settings)
    assert any("Google Sheets credentials" in e for e in errors)


def test_validate_prereqs_sheets_credentials_file_does_not_exist(tmp_path: Path) -> None:
    settings = _full_settings(SHEETS_SERVICE_ACCOUNT_FILE=str(tmp_path / "missing.json"))
    errors = run_local.validate_prereqs(settings)
    assert any("does not exist" in e for e in errors)


def test_validate_prereqs_accepts_inline_google_env_vars() -> None:
    """No SHEETS_SERVICE_ACCOUNT_FILE, but GOOGLE_* vars set — should pass.
    Mirrors the PA-style auth setup the local user actually has."""
    settings = _full_settings(
        SHEETS_SERVICE_ACCOUNT_FILE="",
        GOOGLE_PROJECT_ID="proj",
        GOOGLE_CLIENT_EMAIL="bot@proj.iam.gserviceaccount.com",
        GOOGLE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
        GOOGLE_CLIENT_ID="42",
    )
    errors = run_local.validate_prereqs(settings)
    # The Sheets-credentials error must NOT appear.
    assert not any("Google Sheets" in e for e in errors), errors


def test_validate_prereqs_missing_openai(tmp_path: Path) -> None:
    sa = tmp_path / "sa.json"
    sa.write_text("{}")
    settings = _full_settings(SHEETS_SERVICE_ACCOUNT_FILE=str(sa), OPENAI_API_KEY="")
    errors = run_local.validate_prereqs(settings)
    assert any("OPENAI_API_KEY" in e for e in errors)


def test_validate_prereqs_missing_kie(tmp_path: Path) -> None:
    sa = tmp_path / "sa.json"
    sa.write_text("{}")
    settings = _full_settings(SHEETS_SERVICE_ACCOUNT_FILE=str(sa), KIE_AI_KEYS="")
    errors = run_local.validate_prereqs(settings)
    assert any("KIE_AI_KEYS" in e for e in errors)


def test_validate_prereqs_missing_rendi(tmp_path: Path) -> None:
    sa = tmp_path / "sa.json"
    sa.write_text("{}")
    settings = _full_settings(SHEETS_SERVICE_ACCOUNT_FILE=str(sa), RENDI_API_KEY="")
    errors = run_local.validate_prereqs(settings)
    assert any("RENDI_API_KEY" in e for e in errors)


# ── parse_argv ──────────────────────────────────────────────────────────────


def test_parse_argv_minimal() -> None:
    args = run_local.parse_argv(
        ["--sheet-id", "sid", "--worksheet", "ws", "--layout", "image_vo", "--rows", "5"]
    )
    assert args.sheet_id == "sid"
    assert args.worksheet == "ws"
    assert args.tab == TAB_IMAGE_VO
    assert args.row_nums == [5]
    assert args.concurrency is None
    assert args.log_file is None
    assert args.dry_run is False


def test_parse_argv_layout_is_optional() -> None:
    # --layout is optional — when omitted, args.tab is None and the runner
    # auto-detects the layout from the worksheet name / headers at runtime.
    args = run_local.parse_argv(
        ["--sheet-id", "sid", "--worksheet", "cartoon", "--rows", "5"]
    )
    assert args.tab is None


def test_parse_argv_full() -> None:
    args = run_local.parse_argv(
        [
            "--sheet-id", "sid",
            "--worksheet", "ws",
            "--layout", "cartoon",
            "--rows", "5,7,9-12",
            "--concurrency", "3",
            "--log-file", "C:/tmp/run.log",
            "--dry-run",
        ]
    )
    assert args.tab == TAB_CARTOON
    assert args.row_nums == [5, 7, 9, 10, 11, 12]
    assert args.concurrency == 3
    assert args.log_file == Path("C:/tmp/run.log")
    assert args.dry_run is True


def test_parse_argv_worksheet_is_optional() -> None:
    # No --worksheet means "show the interactive picker at runtime".
    args = run_local.parse_argv(["--sheet-id", "sid"])
    assert args.worksheet is None
    assert args.row_nums is None
    assert args.tab is None


def test_parse_argv_rows_is_optional() -> None:
    # No --rows means "default to all unprocessed in the chosen worksheet".
    args = run_local.parse_argv(["--sheet-id", "sid", "--worksheet", "ws"])
    assert args.row_nums is None


def test_parse_argv_sheet_id_is_optional() -> None:
    # No --sheet-id means "fall back to BULKVID_DEFAULT_SHEET_ID at runtime".
    # All required flags can be omitted — the script handles missing values
    # interactively (or via .env).
    args = run_local.parse_argv([])
    assert args.sheet_id is None
    assert args.worksheet is None
    assert args.row_nums is None


def test_parse_argv_rejects_zero_concurrency() -> None:
    with pytest.raises(SystemExit):
        run_local.parse_argv(
            [
                "--sheet-id", "sid", "--worksheet", "ws", "--layout", "image_vo",
                "--rows", "5", "--concurrency", "0",
            ]
        )


def test_parse_argv_converts_bad_row_range_to_clean_exit() -> None:
    # A reversed range should surface as SystemExit('error: ...'), not a
    # Python traceback the user has to read.
    with pytest.raises(SystemExit) as exc:
        run_local.parse_argv(
            [
                "--sheet-id", "sid", "--worksheet", "ws", "--layout", "image_vo",
                "--rows", "9-5",
            ]
        )
    assert "reversed" in str(exc.value)
    assert str(exc.value).startswith("error: ")


def test_parse_argv_rejects_unknown_tab() -> None:
    with pytest.raises(SystemExit):
        run_local.parse_argv(
            [
                "--sheet-id", "sid", "--worksheet", "ws", "--layout", "bogus",
                "--rows", "5",
            ]
        )


# ── infer_layout_from_name ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name, expected",
    [
        # The Apps Script comment is explicit: "simple x4" -> the 4-video
        # GENERATION flow uses the image_vo column layout, even though the
        # name also contains "simple". x4 wins.
        ("simple x4", TAB_IMAGE_VO),
        ("Simple X4", TAB_IMAGE_VO),
        ("image_vo simple x4 round 2", TAB_IMAGE_VO),
        # Plain "simple" anywhere -> simple.
        ("simple", TAB_SIMPLE),
        ("Simple Vol 3", TAB_SIMPLE),
        ("USA simple campaign", TAB_SIMPLE),
        # "cartoon" anywhere -> cartoon.
        ("cartoon", TAB_CARTOON),
        ("Cartoon Jan 2026", TAB_CARTOON),
        # No signal -> None (caller falls back to header detection).
        ("image_vo", None),
        ("4images_vo2", None),
        ("Random Tab Name", None),
        ("", None),
        ("   ", None),
    ],
)
def test_infer_layout_from_name(name: str, expected: str | None) -> None:
    assert run_local.infer_layout_from_name(name) == expected


# ── infer_layout_from_headers ───────────────────────────────────────────────


def test_infer_layout_from_headers_how_many() -> None:
    headers = ["Country", "Vertical", "Article", "How Many", "Voice Over"]
    assert run_local.infer_layout_from_headers(headers) == TAB_FOUR_IMAGES


def test_infer_layout_from_headers_manual_image() -> None:
    headers = ["Country", "Vertical", "Article", "Manual Image", "Voice Over"]
    assert run_local.infer_layout_from_headers(headers) == TAB_IMAGE_VO


def test_infer_layout_from_headers_case_insensitive() -> None:
    headers = ["COUNTRY", "Vertical", "Article", "MANUAL IMAGE"]
    assert run_local.infer_layout_from_headers(headers) == TAB_IMAGE_VO


def test_infer_layout_from_headers_whitespace_tolerated() -> None:
    headers = ["country ", " article ", "  Manual Image  "]
    assert run_local.infer_layout_from_headers(headers) == TAB_IMAGE_VO


def test_infer_layout_from_headers_how_many_wins_over_manual_image() -> None:
    # If both happen to appear, "How Many" wins -> four_images_vo2 (the
    # 4Images sheet schema). Matches the Apps Script order.
    headers = ["Country", "Article", "How Many", "Manual Image"]
    assert run_local.infer_layout_from_headers(headers) == TAB_FOUR_IMAGES


def test_infer_layout_from_headers_none_for_no_signal() -> None:
    headers = ["Country", "Vertical", "Article", "Voice Over"]
    assert run_local.infer_layout_from_headers(headers) is None


def test_infer_layout_from_headers_handles_blank_cells() -> None:
    headers = ["Country", "", None, "Manual Image"]    # type: ignore[list-item]
    assert run_local.infer_layout_from_headers(headers) == TAB_IMAGE_VO


# ── detect_layout (combined name + headers) ─────────────────────────────────


class _FakeSheetsForDetection:
    """Records header-row reads so tests can prove we DID NOT hit the API
    when name-based detection succeeds."""

    def __init__(self, headers: list[str] | Exception) -> None:
        self._headers = headers
        self.calls: list[tuple[str, str]] = []

    async def read_header_row(self, sheet_id: str, ws: str) -> list[str]:
        self.calls.append((sheet_id, ws))
        if isinstance(self._headers, Exception):
            raise self._headers
        return self._headers


async def test_detect_layout_uses_name_first_no_api_call() -> None:
    sheets = _FakeSheetsForDetection(headers=[])
    layout = await run_local.detect_layout(sheets, "sid", "cartoon round 1")    # type: ignore[arg-type]
    assert layout == TAB_CARTOON
    # Name matched -> no header read.
    assert sheets.calls == []


async def test_detect_layout_falls_back_to_headers() -> None:
    sheets = _FakeSheetsForDetection(headers=["Country", "Article", "How Many"])
    layout = await run_local.detect_layout(sheets, "sid", "USA Q2")    # type: ignore[arg-type]
    assert layout == TAB_FOUR_IMAGES
    assert sheets.calls == [("sid", "USA Q2")]


async def test_detect_layout_raises_when_both_fail() -> None:
    sheets = _FakeSheetsForDetection(headers=["Country", "Vertical", "Article"])
    with pytest.raises(ValueError, match="could not auto-detect"):
        await run_local.detect_layout(sheets, "sid", "weird tab")    # type: ignore[arg-type]


async def test_detect_layout_propagates_sheet_read_errors() -> None:
    # Anything other than ValueError-from-our-helper should bubble up so the
    # caller can show a "could not read sheet" message.
    sheets = _FakeSheetsForDetection(headers=RuntimeError("PERMISSION_DENIED"))
    with pytest.raises(RuntimeError, match="PERMISSION_DENIED"):
        await run_local.detect_layout(sheets, "sid", "weird tab")    # type: ignore[arg-type]


# ── pick_worksheet_from_menu ────────────────────────────────────────────────


def _scripted_input(*responses: str):
    """Build a fake ``input`` function that returns each response in turn."""
    it = iter(responses)
    def _fake(prompt: str = "") -> str:    # noqa: ARG001
        return next(it)
    return _fake


def test_pick_worksheet_by_number() -> None:
    names = ["image_vo", "simple", "cartoon"]
    chosen = run_local.pick_worksheet_from_menu(
        names, input_fn=_scripted_input("2")
    )
    assert chosen == "simple"


def test_pick_worksheet_by_exact_name() -> None:
    names = ["image_vo", "simple", "cartoon"]
    chosen = run_local.pick_worksheet_from_menu(
        names, input_fn=_scripted_input("cartoon")
    )
    assert chosen == "cartoon"


def test_pick_worksheet_case_insensitive_match() -> None:
    names = ["image_vo", "Simple Round 2", "cartoon"]
    chosen = run_local.pick_worksheet_from_menu(
        names, input_fn=_scripted_input("SIMPLE ROUND 2")
    )
    assert chosen == "Simple Round 2"


def test_pick_worksheet_reprompts_on_bad_input() -> None:
    names = ["image_vo", "simple", "cartoon"]
    chosen = run_local.pick_worksheet_from_menu(
        names, input_fn=_scripted_input("nope", "99", "3")
    )
    assert chosen == "cartoon"


def test_pick_worksheet_quit() -> None:
    names = ["image_vo"]
    with pytest.raises(SystemExit) as exc:
        run_local.pick_worksheet_from_menu(
            names, input_fn=_scripted_input("q")
        )
    assert exc.value.code == 0


def test_pick_worksheet_empty_list_raises() -> None:
    with pytest.raises(ValueError, match="no worksheet tabs"):
        run_local.pick_worksheet_from_menu([], input_fn=_scripted_input())


# ── pick_rows_from_default ──────────────────────────────────────────────────


def test_pick_rows_enter_takes_all_unprocessed() -> None:
    rows = run_local.pick_rows_from_default(
        [3, 5, 7, 9], input_fn=_scripted_input("")
    )
    assert rows == [3, 5, 7, 9]


def test_pick_rows_override_with_range() -> None:
    rows = run_local.pick_rows_from_default(
        [3, 5, 7, 9, 11, 13], input_fn=_scripted_input("5,9-11")
    )
    assert rows == [5, 9, 10, 11]


def test_pick_rows_reprompts_on_bad_range() -> None:
    rows = run_local.pick_rows_from_default(
        [3, 5, 7], input_fn=_scripted_input("abc", "5")
    )
    assert rows == [5]


def test_pick_rows_quit() -> None:
    with pytest.raises(SystemExit) as exc:
        run_local.pick_rows_from_default(
            [3, 5], input_fn=_scripted_input("q")
        )
    assert exc.value.code == 0


def test_pick_rows_empty_unprocessed_returns_empty() -> None:
    # Caller is responsible for deciding what to do with an empty set; the
    # picker just signals "no choice to be made".
    rows = run_local.pick_rows_from_default([], input_fn=_scripted_input())
    assert rows == []


# ── run_batch (the concurrency loop) ────────────────────────────────────────


class _FakeSheetsWrites:
    """Record every batch_write_video_urls call and the writes seen."""

    def __init__(self) -> None:
        self.write_calls: list[list] = []

    async def batch_write_video_urls(self, writes: list) -> int:
        # Mimic the real adapter — one cell per video URL.
        self.write_calls.append(list(writes))
        return sum(len(w.video_urls) for w in writes)


async def test_run_batch_dispatches_each_row_and_writes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheets = _FakeSheetsWrites()
    seen_rows: list[int] = []

    async def _fake_process(tab, row, clients, *, job_id):    # noqa: ARG001
        seen_rows.append(row.row_num)
        return RowResult(
            row_num=row.row_num,
            status=STATUS_SUCCESS,
            video_urls=[f"u{row.row_num}-1", f"u{row.row_num}-2"],
            cost_usd=0.1,
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr(run_local, "process_one_row", _fake_process)

    rows = [_image_vo_row(), ImageVORow(
        row_num=6, country="", vertical="", article_url="http://a",
        manual_image_url="http://i", voice_over=True, zapcap=False,
        aspect_ratio="9:16", script_pattern="", open_comments="",
    )]

    outcome = await run_local.run_batch(
        rows,    # type: ignore[arg-type]
        tab=TAB_IMAGE_VO,
        clients=None,    # type: ignore[arg-type]
        sheets=sheets,    # type: ignore[arg-type]
        sheet_id="sid",
        worksheet="ws",
        job_id="job-1",
        concurrency=2,
    )

    assert outcome.succeeded == 2
    assert outcome.failed == 0
    assert outcome.total_cost_usd == pytest.approx(0.2)
    # One write call per finished row.
    assert len(sheets.write_calls) == 2
    # Each call wrote exactly one PendingWrite.
    assert all(len(c) == 1 for c in sheets.write_calls)
    # Both rows were dispatched.
    assert sorted(seen_rows) == [5, 6]


async def test_run_batch_handles_exception_as_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheets = _FakeSheetsWrites()

    async def _boom(tab, row, clients, *, job_id):    # noqa: ARG001
        raise RuntimeError("kaboom")

    monkeypatch.setattr(run_local, "process_one_row", _boom)

    outcome = await run_local.run_batch(
        [_image_vo_row()],    # type: ignore[arg-type]
        tab=TAB_IMAGE_VO,
        clients=None,    # type: ignore[arg-type]
        sheets=sheets,    # type: ignore[arg-type]
        sheet_id="sid",
        worksheet="ws",
        job_id="job-1",
        concurrency=1,
    )
    assert outcome.succeeded == 0
    assert outcome.failed == 1
    # Even the failure is written back so the user sees it in the Sheet.
    assert len(sheets.write_calls) == 1
    pw = sheets.write_calls[0][0]
    assert pw.status == STATUS_INTERNAL_ERROR
    assert pw.error is not None and "kaboom" in pw.error
    assert pw.video_urls == []


async def test_run_batch_respects_concurrency_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheets = _FakeSheetsWrites()
    in_flight = 0
    max_seen = 0
    started = asyncio.Event()

    async def _slow(tab, row, clients, *, job_id):    # noqa: ARG001
        nonlocal in_flight, max_seen
        in_flight += 1
        max_seen = max(max_seen, in_flight)
        started.set()
        await asyncio.sleep(0.05)
        in_flight -= 1
        return RowResult(row_num=row.row_num, status=STATUS_SUCCESS, video_urls=[])

    monkeypatch.setattr(run_local, "process_one_row", _slow)

    rows = [
        ImageVORow(
            row_num=n, country="", vertical="", article_url="http://a",
            manual_image_url="http://i", voice_over=True, zapcap=False,
            aspect_ratio="9:16", script_pattern="", open_comments="",
        )
        for n in range(2, 12)    # 10 rows
    ]

    outcome = await run_local.run_batch(
        rows,    # type: ignore[arg-type]
        tab=TAB_IMAGE_VO,
        clients=None,    # type: ignore[arg-type]
        sheets=sheets,    # type: ignore[arg-type]
        sheet_id="sid",
        worksheet="ws",
        job_id="job-1",
        concurrency=3,
    )
    assert outcome.succeeded == 10
    # Never more than 3 in flight at once.
    assert max_seen == 3


async def test_run_batch_keeps_going_when_writeback_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the sheet write blows up, the row result still counts toward
    # succeeded/failed; the loop survives.

    class _BrokenSheets:
        async def batch_write_video_urls(self, writes):    # noqa: ARG002
            raise RuntimeError("sheet api down")

    async def _ok(tab, row, clients, *, job_id):    # noqa: ARG001
        return RowResult(
            row_num=row.row_num, status=STATUS_SUCCESS, video_urls=["u"]
        )

    monkeypatch.setattr(run_local, "process_one_row", _ok)

    outcome = await run_local.run_batch(
        [_image_vo_row()],    # type: ignore[arg-type]
        tab=TAB_IMAGE_VO,
        clients=None,    # type: ignore[arg-type]
        sheets=_BrokenSheets(),    # type: ignore[arg-type]
        sheet_id="sid",
        worksheet="ws",
        job_id="job-1",
        concurrency=1,
    )
    assert outcome.succeeded == 1
    assert outcome.failed == 0


# ── make_job_id ─────────────────────────────────────────────────────────────


def test_make_job_id_has_expected_shape() -> None:
    jid = run_local.make_job_id()
    assert jid.startswith("local-")
    # Format: local-<host>-<YYYYMMDDTHHMMSSZ>
    parts = jid.split("-")
    assert len(parts) >= 3
    ts = parts[-1]
    assert ts.endswith("Z")
    assert len(ts) == len("YYYYMMDDTHHMMSSZ")
