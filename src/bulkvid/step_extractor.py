"""Pipeline-step extractor for the sidebar's live row status.

Given a job + row, peek at the tail of the per-job log file and figure
out which pipeline step the worker is currently inside. The sidebar
renders the result instead of the generic "working…" placeholder so the
user can see what's actually happening (article fetch → script → image
gen → video assembly → upload, etc.).

We deliberately do this server-side from log events rather than adding
a ``current_step`` column to ``row_queue`` and threading writes through
every pipeline adapter: the log file is already the source of truth and
this is a UI-only feature — no need to bloat the data model for it.

Plan: ``_plans/2026-06-04-sidebar-ux-overhaul.md`` §Phase 1.
"""

from __future__ import annotations

import json
from pathlib import Path

from bulkvid.config import get_settings


# Closed set of log event names → human-readable step labels. The
# matcher walks the log tail newest-line-first and returns the FIRST
# entry whose ``event`` is in this dict. So later steps in the pipeline
# (e.g. ``rendi_poll_pending``) outrank earlier ones
# (e.g. ``article_fetch_ok``) automatically — no ordering logic needed.
#
# Plain + informative tone per Yoav's pick on the plan question.
STEP_FROM_EVENT: dict[str, str] = {
    # Article fetch
    "article_tavily_submit":      "Fetching article (Tavily)",
    "article_tavily_failed":      "Fetching article (Tavily failed, falling back)",
    "article_scrapingbee_submit": "Fetching article (ScrapingBee)",
    "article_fetch_ok":           "Article fetched",
    # Language + safety
    "detect_submit":              "Detecting language",
    "detect_ok":                  "Language detected",
    "safety_detect":              "Running safety check",
    # Script generation
    "script_submit":              "Writing script",
    "script_ok":                  "Script ready",
    # Cartoon-specific planner
    "cartoon_plan_submit":        "Planning cartoon shots",
    "cartoon_plan_ok":            "Cartoon plan ready",
    "cartoon_shorten_submit":     "Shortening voiceover",
    # TTS
    "tts_synthesize":             "Synthesizing voice",
    "tts_synthesize_ok":          "Voice ready",
    # Image generation
    "describe_submit":            "Describing seed image",
    "describe_ok":                "Seed image described",
    "collage_prompt_submit":      "Building image prompt",
    "collage_prompt_ok":          "Image prompt ready",
    "kie_submit":                 "Generating image",
    "kie_poll_pending":           "Generating image",
    "kie_poll_ok":                "Image ready",
    "kie_poll_fail":              "Image filtered — retrying with fallback",
    "nano_banana_2_failed_falling_back": "Falling back to GPT-image",
    # Seedance (cartoon mode)
    "seedance_image_submit":      "Generating cartoon shot",
    "seedance_image_ok":          "Cartoon shot ready",
    "seedance_video_submit":      "Animating cartoon shot",
    "seedance_video_ok":          "Cartoon shot animated",
    # Video assembly
    "rendi_submit":               "Assembling video",
    "rendi_poll_pending":         "Assembling video",
    "rendi_poll_ok":              "Video assembled",
    # Subtitles
    "zapcap_submit":              "Adding subtitles",
    "zapcap_poll_pending":        "Adding subtitles",
    "zapcap_poll_ok":             "Subtitles added",
    # Storage
    "gcs_upload":                 "Uploading",
    "gcs_upload_ok":              "Uploaded",
    "s3_upload":                  "Uploading (S3)",
    "s3_upload_ok":               "Uploaded (S3)",
    # Terminal
    "row_start":                  "Starting",
    "row_done":                   "Done",
    "row_failed":                 "Failed",
}


# Tail size — we look at the last N log lines, not the whole file. 200
# is comfortably larger than any single row's expected event count
# (which is ~15-30 for a typical pipeline run) and keeps the parse cost
# at a few ms even for a multi-day-old job.
_TAIL_LINES = 200


def extract_current_step(job_id: str, row_num: int | None) -> str | None:
    """Return the human-readable label for the most recent known
    pipeline event for ``(job_id, row_num)``, or ``None`` if the log
    file doesn't exist yet / has no recognised events.

    Walks the log tail in REVERSE (newest first) and returns on the
    first event whose name is in ``STEP_FROM_EVENT`` — newest matching
    event wins, no comparator logic needed.

    Path sanitization mirrors ``read_job_log_lines`` so a malformed
    ``job_id`` can't traverse outside the logs dir.
    """
    safe = str(job_id).replace("/", "_").replace("\\", "_").replace("..", "_")
    path = Path(get_settings().BULKVID_DATA_DIR) / "logs" / f"{safe}.log"
    if not path.exists():
        return None

    # ``splitlines`` over the whole file is cheap for our log sizes
    # (typical jobs produce <100 KB of per-job log). If it ever becomes
    # a hot path we can switch to a seek-from-end tail.
    raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for raw in reversed(raw_lines[-_TAIL_LINES:]):
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if row_num is not None:
            entry_row = entry.get("row_num")
            if entry_row is None:
                continue
            try:
                if int(entry_row) != int(row_num):
                    continue
            except (TypeError, ValueError):
                continue
        event = entry.get("event", "")
        if event in STEP_FROM_EVENT:
            return STEP_FROM_EVENT[event]
    return None
