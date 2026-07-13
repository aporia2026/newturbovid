"""Bundled background-music library for the hook_card tab.

The hook_card video plays a royalty-free instrumental under the slideshow.
Tracks live in ``src/bulkvid/assets/music/`` and are named ``<name>_<n>.mp3``
where ``<name>`` is a short 1-2 word style label and ``<n>`` is the variation.
The sheet's Music column (col F) selects one:

  - ``"Uplifting 2"`` (name + variation) -> that exact track.
  - ``"Uplifting"``   (name only)        -> a random variation of that style.
  - ``"None"``                            -> no music (silent video).
  - blank                                 -> a random track from the whole pool.

An unknown name (typo) also falls back to a random track. Name matching is
case- and separator-insensitive ("Lo-Fi" / "lo fi" / "lofi" all match).

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
# Trailing ``_<n>`` on a file stem (uplifting_2 -> variation 2, base "uplifting").
_FILE_VARIATION_RE = re.compile(r"_(\d+)$")
# Trailing variation on a REQUEST value, allowing a space/underscore/hyphen or
# nothing before the number ("Uplifting 2", "uplifting_2", "Uplifting2").
_REQUEST_VARIATION_RE = re.compile(r"[ _-]?(\d+)$")


def _name_key(text: str) -> str:
    """Normalize a name for matching: lowercase alphanumerics only, so
    ``"Lo-Fi"``, ``"lo fi"`` and ``"lofi"`` all collapse to the same key."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def _base_name(path: Path) -> str:
    """The style name of a track file, minus the ``_<n>`` variation suffix."""
    return _FILE_VARIATION_RE.sub("", path.stem)


def _file_variation(path: Path) -> int | None:
    m = _FILE_VARIATION_RE.search(path.stem)
    return int(m.group(1)) if m else None


def _parse_request(text: str) -> tuple[str, int | None]:
    """Split a Music value into ``(name key, variation or None)``.

    ``"Uplifting 2"`` -> ``("uplifting", 2)``; ``"Uplifting"`` ->
    ``("uplifting", None)``; ``""`` -> ``("", None)``.
    """
    t = (text or "").strip()
    m = _REQUEST_VARIATION_RE.search(t)
    if m:
        return _name_key(t[: m.start()]), int(m.group(1))
    return _name_key(t), None


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
    """Distinct style names (first-seen spelling wins), sorted."""
    seen: dict[str, str] = {}
    for p in list_tracks():
        base = _base_name(p)
        seen.setdefault(_name_key(base), base)
    return sorted(seen.values(), key=str.lower)


def track_choices() -> list[str]:
    """Every pickable value — each track as ``"<Name> <n>"`` (title-cased) —
    so the sheet dropdown can be kept in sync with the actual files."""
    out: list[str] = []
    for p in list_tracks():
        base = _base_name(p)
        label = base[:1].upper() + base[1:]
        v = _file_variation(p)
        out.append(f"{label} {v}" if v is not None else label)
    return out


def select_track(name: str | None = None, *, rng: random.Random | None = None) -> Path | None:
    """Pick a bundled track for a Music-column value.

    ``"<name> <n>"`` -> that exact variation; ``"<name>"`` -> a random variation
    of that style; ``"None"`` -> no music (silent); blank -> a random track from
    the whole pool. An unknown name, or a variation that does not exist, falls
    back to a random choice. Returns None when the operator chose "None" or no
    tracks are bundled.
    """
    key, variation = _parse_request(name or "")
    if key == "none":
        return None    # operator picked "None" -> silent video
    tracks = list_tracks()
    if not tracks:
        _log.warning("hook_card_music_empty", music_dir=str(MUSIC_DIR))
        return None
    chooser = rng or random
    if key:
        by_name = [p for p in tracks if _name_key(_base_name(p)) == key]
        if by_name:
            if variation is not None:
                exact = [p for p in by_name if _file_variation(p) == variation]
                if exact:
                    return exact[0]
            return chooser.choice(by_name)    # name only, or no such variation
        _log.warning("hook_card_music_name_not_found", requested=name)
    return chooser.choice(tracks)


def content_type_for(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
