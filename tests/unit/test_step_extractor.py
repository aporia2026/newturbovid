"""Tests for the sidebar's per-row pipeline-step extractor.

Plan: ``_plans/2026-06-04-sidebar-ux-overhaul.md`` §Phase 1.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bulkvid import step_extractor


@pytest.fixture(autouse=True)
def _redirect_data_dir(tmp_path, monkeypatch) -> Path:
    """All tests use a per-test ``BULKVID_DATA_DIR``. We patch the
    Settings instance directly via ``get_settings`` so the lru_cache
    interaction stays out of test code."""
    from bulkvid import config as cfg

    real_settings = cfg.Settings(BULKVID_DATA_DIR=tmp_path)    # type: ignore[call-arg]
    monkeypatch.setattr(cfg, "get_settings", lambda: real_settings)
    # step_extractor imports ``get_settings`` at module load time, so
    # the lambda also has to land on the module's local reference.
    monkeypatch.setattr(step_extractor, "get_settings", lambda: real_settings)
    yield tmp_path


def _write_log(data_dir: Path, job_id: str, entries: list[dict]) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{job_id}.log"
    log_path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )


# ── Happy paths ────────────────────────────────────────────────────────────


def test_extracts_last_matching_event(_redirect_data_dir: Path) -> None:
    """Newer log lines outrank older ones — the matcher walks the tail
    in reverse and returns on the first hit."""
    _write_log(
        _redirect_data_dir,
        "job-A",
        [
            {"event": "article_fetch_ok", "row_num": 5},
            {"event": "script_submit", "row_num": 5},
            {"event": "tts_synthesize", "row_num": 5},
        ],
    )
    assert step_extractor.extract_current_step("job-A", 5) == "Synthesizing voice"


def test_filters_by_row_num(_redirect_data_dir: Path) -> None:
    """A multi-row job has events for row 2 AND row 3 interleaved.
    Extracting row 2's step must ignore row 3's events."""
    _write_log(
        _redirect_data_dir,
        "job-B",
        [
            {"event": "article_fetch_ok", "row_num": 2},
            {"event": "rendi_submit", "row_num": 3},     # row 3 is further along
            {"event": "script_submit", "row_num": 2},    # row 2 is mid-script
        ],
    )
    assert step_extractor.extract_current_step("job-B", 2) == "Writing script"
    assert step_extractor.extract_current_step("job-B", 3) == "Assembling video"


def test_unknown_event_falls_through_to_older_known_event(
    _redirect_data_dir: Path,
) -> None:
    """The newest line is some random debug event we don't recognise.
    The extractor should skip it and return the last KNOWN event's step."""
    _write_log(
        _redirect_data_dir,
        "job-C",
        [
            {"event": "tts_synthesize_ok", "row_num": 1},
            {"event": "some_debug_event_we_dont_care_about", "row_num": 1},
        ],
    )
    assert step_extractor.extract_current_step("job-C", 1) == "Voice ready"


# ── Edge cases & safety ────────────────────────────────────────────────────


def test_missing_log_file_returns_none(_redirect_data_dir: Path) -> None:
    assert step_extractor.extract_current_step("job-missing", 1) is None


def test_empty_log_file_returns_none(_redirect_data_dir: Path) -> None:
    _write_log(_redirect_data_dir, "job-empty", [])
    assert step_extractor.extract_current_step("job-empty", 1) is None


def test_log_with_no_matching_events_returns_none(_redirect_data_dir: Path) -> None:
    _write_log(
        _redirect_data_dir,
        "job-noise",
        [
            {"event": "random_debug", "row_num": 1},
            {"event": "another_unknown", "row_num": 1},
        ],
    )
    assert step_extractor.extract_current_step("job-noise", 1) is None


def test_malformed_json_lines_are_skipped(_redirect_data_dir: Path) -> None:
    """A truncated log line (write happened mid-flush) shouldn't blow up
    the extractor — just skip the bad line and keep looking."""
    log_dir = _redirect_data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "job-mixed.log"
    log_path.write_text(
        json.dumps({"event": "script_submit", "row_num": 1}) + "\n"
        + "this line is not json at all\n"
        + json.dumps({"event": "tts_synthesize", "row_num": 1}) + "\n",
        encoding="utf-8",
    )
    assert step_extractor.extract_current_step("job-mixed", 1) == "Synthesizing voice"


def test_path_traversal_in_job_id_is_neutralised(_redirect_data_dir: Path) -> None:
    """A malicious job_id can't escape the logs dir. The extractor
    mirrors ``read_job_log_lines``'s sanitization — slashes and ``..``
    are replaced with ``_``. We compute the sanitized filename the same
    way the extractor does and write the log there, then assert that the
    malicious-looking input resolves to it (not to a path outside the
    logs dir)."""
    malicious_id = "../../escape/job"
    sanitized = (
        malicious_id.replace("/", "_").replace("\\", "_").replace("..", "_")
    )
    _write_log(
        _redirect_data_dir,
        sanitized,
        [{"event": "tts_synthesize_ok", "row_num": 1}],
    )
    assert (
        step_extractor.extract_current_step(malicious_id, 1) == "Voice ready"
    )


def test_row_num_none_matches_any_row(_redirect_data_dir: Path) -> None:
    """If the caller doesn't filter by row, the extractor returns the
    newest known event across all rows. Useful for single-row jobs."""
    _write_log(
        _redirect_data_dir,
        "job-anyrow",
        [
            {"event": "article_fetch_ok", "row_num": 5},
            {"event": "script_submit", "row_num": 5},
        ],
    )
    assert step_extractor.extract_current_step("job-anyrow", None) == "Writing script"


def test_step_map_covers_pipeline_terminal_states() -> None:
    """Defense: the matcher must know about ``row_done`` / ``row_failed``
    so the sidebar can render a meaningful step for the brief window
    between the worker writing the terminal event and the row_queue
    status flipping."""
    assert step_extractor.STEP_FROM_EVENT["row_done"] == "Done"
    assert step_extractor.STEP_FROM_EVENT["row_failed"] == "Failed"
