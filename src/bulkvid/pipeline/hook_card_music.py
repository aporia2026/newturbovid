"""Bundled background-music library for the hook_card tab.

The hook_card video plays a royalty-free instrumental under the slideshow.
Tracks live in ``src/bulkvid/assets/music/`` and are named ``<name>_<n>.mp3``
where ``<name>`` is a short 1-2 word label the operator picks in the sheet's
Music column (col F) and ``<n>`` is the variation. Selection:

  - Music cell filled  -> play a track whose name matches (a random variation
    of that name; a name with no match falls back to a random track).
  - Music cell blank    -> play a random track from the whole pool.

The pool is generated once with ``tools/generate_hook_card_music.py`` (Suno via
kie.ai) — instrumental, no vocals. See the folder README for the licensing note.
If the folder is empty the row processor renders the video SILENT (still valid)
and logs a warning, rather than failing the row.

Plan: ``_plans/2026-07-13-hook-card-tab.md``.
"""

from __future__ import annotations

import random
import re
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
_VARIATION_RE = re.compile(r"_\d+$")


def _name_key(text: str) -> str:
    """Normalize a track name for matching: lowercase alphanumerics only, so
    ``"Lo-Fi"``, ``"lo fi"`` and ``"lofi"`` all collapse to the same key."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def _base_name(path: Path) -> str:
    """The display name of a track file, minus the ``_<n>`` variation suffix."""
    return _VARIATION_RE.sub("", path.stem)


def list_tracks() -> list[Path]:
    """All bundled audio tracks, sorted by name for a stable order."""
    if not MUSIC_DIR.is_dir():
        return []
    return sorted(
        p
        for p in MUSIC_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTS
    )


def track_names() -> list[str]:
    """Distinct pickable track names (first-seen spelling wins), sorted."""
    seen: dict[str, str] = {}
    for p in list_tracks():
        base = _base_name(p)
        seen.setdefault(_name_key(base), base)
    return sorted(seen.values(), key=str.lower)


def select_track(name: str | None = None, *, rng: random.Random | None = None) -> Path | None:
    """Pick a bundled track.

    ``name`` given -> a random variation of the track with that name; if no
    track matches, fall back to a random track (a typo still yields music).
    ``name`` blank/None -> a random track from the whole pool. Returns None when
    no tracks are bundled.
    """
    tracks = list_tracks()
    if not tracks:
        _log.warning("hook_card_music_empty", music_dir=str(MUSIC_DIR))
        return None
    chooser = rng or random
    wanted = _name_key(name or "")
    if wanted:
        matching = [p for p in tracks if _name_key(_base_name(p)) == wanted]
        if matching:
            return chooser.choice(matching)
        _log.warning("hook_card_music_name_not_found", requested=name)
    return chooser.choice(tracks)


def content_type_for(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
