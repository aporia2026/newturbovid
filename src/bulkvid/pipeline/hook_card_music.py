"""Bundled background-music library for the hook_card tab.

The hook_card video plays a royalty-free track under the slideshow. Tracks live
in ``src/bulkvid/assets/music/`` and are picked deterministically per row so a
batch rotates through the library instead of repeating one song.

IMPORTANT (licensing): only platform-cleared audio may go in that folder — the
Meta Sound Collection, the TikTok Commercial Music Library, or the YouTube
Audio Library "no attribution" tracks. "Royalty-free" alone is not enough for
paid ads; the platforms run their own Content-ID fingerprinting and can claim
a technically-free track. Keep per-track provenance. See the folder README.

If the folder is empty the row processor renders the video SILENT (still a
valid clip) and logs a warning, rather than failing the row.

Plan: ``_plans/2026-07-13-hook-card-tab.md``.
"""

from __future__ import annotations

from pathlib import Path

from bulkvid.logging import get_logger

_log = get_logger("hook_card_music")

MUSIC_DIR = Path(__file__).resolve().parent.parent / "assets" / "music"

_AUDIO_EXTS = frozenset({".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus"})
_CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
}


def list_tracks() -> list[Path]:
    """All bundled audio tracks, sorted by name for a stable rotation order."""
    if not MUSIC_DIR.is_dir():
        return []
    return sorted(
        p
        for p in MUSIC_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTS
    )


def select_track(seed: int) -> Path | None:
    """Pick one track deterministically from ``seed`` (e.g. the row number) so a
    batch rotates through the library. Returns None when none are bundled."""
    tracks = list_tracks()
    if not tracks:
        _log.warning("hook_card_music_empty", music_dir=str(MUSIC_DIR))
        return None
    return tracks[seed % len(tracks)]


def content_type_for(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
