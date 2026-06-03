"""Row-level data classes shared by both row processors.

The Sheet payloads land here after parsing; the orchestrator returns a
``RowResult`` with everything the sheet-writer needs to fill the row plus
metadata for the SYMPHONY_DB log.

Plan §15 Appendix A (sheet column maps) and Phase 7 (metadata).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Status codes — one per failure mode the orchestrator can return.
STATUS_SUCCESS = "SUCCESS"
STATUS_ARTICLE_FETCH_FAILED = "ARTICLE_FETCH_FAILED"
STATUS_IMAGE_DOWNLOAD_FAILED = "IMAGE_DOWNLOAD_FAILED"
STATUS_IMAGE_GEN_FAILED = "IMAGE_GEN_FAILED"
STATUS_TTS_FAILED = "TTS_FAILED"
STATUS_VIDEO_ASSEMBLY_FAILED = "VIDEO_ASSEMBLY_FAILED"
STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS = "ZAPCAP_FAILED_KEPT_NO_CAPTIONS"
STATUS_STORAGE_FAILED = "STORAGE_FAILED"
STATUS_INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass
class ImageVORow:
    """Image-VO tab input row (plan §15 Appendix A)."""

    row_num: int
    country: str
    vertical: str
    article_url: str
    manual_image_url: str
    voice_over: bool                  # default True
    zapcap: bool                      # default False on this tab
    aspect_ratio: str                 # e.g. "9:16"
    script_pattern: str               # e.g. "How To"
    open_comments: str                # highest-priority signal


@dataclass
class SimpleRow:
    """Simple tab input row — one video from the user's existing Manual Image.

    Same input columns as Image-VO, but NO image generation: the supplied
    ``manual_image_url`` is resized to the target aspect and turned into a
    single voiceover video. Exactly one ``Ready Video`` is written back.
    """

    row_num: int
    country: str
    vertical: str
    article_url: str
    manual_image_url: str
    voice_over: bool                  # default True
    zapcap: bool                      # default False
    aspect_ratio: str                 # e.g. "9:16"
    script_pattern: str
    open_comments: str


@dataclass
class FourImagesVO2Row:
    """4Images-VO2 tab input row (plan §15 Appendix A)."""

    row_num: int
    country: str
    vertical: str
    article_url: str
    how_many: int                     # 1..4
    voice_over: bool                  # default True
    image_urls: list[str]             # exactly how_many URLs
    zapcap: bool
    aspect_ratio: str
    script_pattern: str
    open_comments: str


@dataclass
class RowResult:
    """What the row processor hands back to the sheet writer + metadata log."""

    row_num: int
    status: str
    video_urls: list[str] = field(default_factory=list)   # Ready Video 1..4
    cost_usd: float = 0.0
    elapsed_seconds: float = 0.0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
