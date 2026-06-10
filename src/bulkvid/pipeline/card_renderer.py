"""Pillow-based card renderer for the ``simple x4`` template feature.

Three templates ship today (mockups under
``apps_script/template_previews/``):

  - Template 1 (blue/purple): image fills the top, white strip at the bottom
    with a bold purple headline centered and a pink rounded CTA pill below.
  - Template 2 (green gradient): image fills the canvas, a vertical
    green-to-dark-green gradient overlays the lower portion to keep the
    headline readable; bold white headline + yellow rounded CTA pill.
  - Template 3 (navy + red): image cover-crops the top 75%, a thin red
    separator line, a deep-navy band with the white bold all-caps title,
    and a bright-red full-width CTA pill with yellow text at the bottom.

Plan: ``_plans/2026-06-08-simple-x4-template-cards.md`` §D.2 (R1 chosen),
§D.6 (renderer scales to any aspect ratio).
Template 3: ``_plans/2026-06-08-simple-x4-template-3.md``.

Font resolution
---------------
Inter Bold ships in the repo (``src/bulkvid/assets/fonts/Inter-Bold.ttf``,
SIL OFL). It's the closest free font to the user-supplied mockups (modern
geometric bold sans with full Latin Extended for accents like "é") and gives
deterministic output across every deploy target. The system-font fallback
chain stays as a safety net in case the bundled file is ever missing.
"""

from __future__ import annotations

import io
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

from bulkvid.logging import get_logger

_log = get_logger("card_render")

# Complex-script shaping (Thai mark stacking, RTL reordering, Arabic joining)
# needs Pillow's raqm engine. The wheels bundle it, but it only activates when
# system libfribidi is present — the Dockerfile installs libfribidi0 for this.
# Logged once at import so a deploy missing it shows up in the HF logs instead
# of silently shipping reversed Hebrew / unshaped Thai.
_log.info("text_shaping_engine", raqm_available=features.check("raqm"))


# ── Public types ─────────────────────────────────────────────────────────────


TEMPLATE_1: Final[str] = "1"
TEMPLATE_2: Final[str] = "2"
TEMPLATE_3: Final[str] = "3"
SUPPORTED_TEMPLATES: Final[frozenset[str]] = frozenset(
    {TEMPLATE_1, TEMPLATE_2, TEMPLATE_3}
)


@dataclass(frozen=True)
class CardDesign:
    """One template's visual identity. New templates = new instance + dispatch."""

    template_id: str
    # Bottom strip height as a fraction of canvas height. Template 1 uses a
    # white strip below the image; template 2 uses a gradient overlay.
    strip_height_frac: float
    # Background image cover height (fraction of canvas). For template 1 the
    # image stops above the strip; for template 2 the image fills the whole
    # canvas and the gradient overlays the lower portion.
    image_cover_frac: float
    # Title text color (R,G,B).
    title_color: tuple[int, int, int]
    # CTA pill colors.
    cta_bg: tuple[int, int, int]
    cta_text_color: tuple[int, int, int]
    # Background color of the bottom strip (only for template 1; template 2
    # uses the gradient instead).
    strip_bg: tuple[int, int, int] | None
    # Thin accent line under the image (only template 1).
    accent_line_color: tuple[int, int, int] | None
    # Optional gradient start/end colors for template 2.
    gradient_top: tuple[int, int, int] | None
    gradient_bottom: tuple[int, int, int] | None


_DESIGNS: Final[dict[str, CardDesign]] = {
    TEMPLATE_1: CardDesign(
        template_id=TEMPLATE_1,
        strip_height_frac=0.22,
        image_cover_frac=0.78,
        title_color=(120, 60, 235),       # vivid purple from mockup
        cta_bg=(255, 50, 100),            # pink/red pill
        cta_text_color=(255, 255, 255),
        strip_bg=(255, 255, 255),
        accent_line_color=(255, 50, 100),
        gradient_top=None,
        gradient_bottom=None,
    ),
    TEMPLATE_2: CardDesign(
        template_id=TEMPLATE_2,
        strip_height_frac=0.32,
        image_cover_frac=1.00,            # image fills, gradient overlays
        title_color=(255, 255, 255),      # white over gradient
        cta_bg=(255, 195, 30),            # yellow pill
        cta_text_color=(20, 20, 20),
        strip_bg=None,
        accent_line_color=None,
        gradient_top=(40, 220, 50),       # bright green top
        gradient_bottom=(5, 60, 5),       # dark green/black bottom
    ),
    TEMPLATE_3: CardDesign(
        template_id=TEMPLATE_3,
        # Total non-image area = navy band + red pill = 25% of canvas.
        # The renderer splits this into 17% band + 8% pill (see
        # ``_render_template_3`` for the constants that carve up
        # ``strip_height_frac``).
        strip_height_frac=0.25,
        image_cover_frac=0.75,
        title_color=(255, 255, 255),      # white on deep navy
        cta_bg=(230, 30, 35),             # bright red full-width pill
        cta_text_color=(255, 215, 0),     # yellow CTA text on red pill
        strip_bg=(15, 30, 55),            # deep navy band behind title
        accent_line_color=(230, 30, 35),  # red separator under image
        gradient_top=None,
        gradient_bottom=None,
    ),
}


# ── Font resolution ──────────────────────────────────────────────────────────


_BUNDLED_FONT_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Inter-Variable.ttf"
)

# Variable-font axes for Bold display weight. ``opsz`` (14) = display-optimized
# glyph shapes; ``wght`` (700) = Bold. Pillow's set_variation_by_axes() expects
# values in the axis order the font declares — for Inter that's [opsz, wght].
_BUNDLED_FONT_AXES: Final[tuple[float, float]] = (14.0, 700.0)

# Legacy static Bold variant (basic Latin only — missing Polish ł, ę etc.).
# Kept as a final fallback for the unlikely case the variable font is missing
# or its variation API isn't available; never picked when the variable font
# is present. Will be removed once the variable font has shipped for a while.
_LEGACY_BUNDLED_FONT_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Inter-Bold.ttf"
)

_SYSTEM_FONT_CANDIDATES: Final[tuple[str, ...]] = (
    # Linux deploy targets first (production matters more than dev).
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    # Windows dev box.
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    # macOS dev box.
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
)


# ── Template-3 display fonts ────────────────────────────────────────────────
#
# T3's mockup uses a heavy condensed display sans (Anton/Bebas-style), not
# Inter's standard-width Bold. Inter has no width axis so no variation can
# match the mockup; the only fix is to swap fonts.
#
# Anton (single weight, Latin + Latin Extended + Vietnamese) is the closest
# free match. Three script-specific fallbacks cover the languages Anton
# doesn't — chosen so each fallback is the closest "heavy condensed display
# sans" available for its script:
#
#   * Cyrillic (Russian, Ukrainian, Bulgarian, …) → Oswald Variable @ 700
#   * Hebrew                                       → Heebo Variable @ 900
#   * Arabic                                       → Cairo Variable @ 900
#
# The picker (``_pick_template_3_font_path``) scans the headline for the first
# character belonging to one of those scripts and routes the whole render
# through the matching font. Latin (incl. Vietnamese) falls through to Anton.
# All four fonts ship under the SIL Open Font License.
_T3_FONT_LATIN: Final[Path] = (
    Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Anton-Regular.ttf"
)
_T3_FONT_CYRILLIC: Final[Path] = (
    Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Oswald-Variable.ttf"
)
_T3_FONT_HEBREW: Final[Path] = (
    Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Heebo-Variable.ttf"
)
_T3_FONT_ARABIC: Final[Path] = (
    Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Cairo-Variable.ttf"
)


# ── Script fonts (Thai / CJK / Devanagari) ──────────────────────────────────
#
# Inter covers Latin (+Ext), Cyrillic, Greek and Vietnamese — and nothing
# else. The sheet carries TH, JP, HK, KR and IL rows (tofu screenshots, chat
# 2026-06-10); the Noto fonts below (SIL OFL, same license as the rest of
# the bundle) cover every script an ad market could realistically add:
# Thai, Japanese, Traditional + Simplified Chinese, Korean and Devanagari
# (Hindi). Hebrew and Arabic reuse the Heebo/Cairo files that already ship
# for Template 3. Anything still uncovered is caught loudly at render time
# by ``_warn_if_missing_glyphs`` instead of shipping silent tofu.
# Plan: _plans/2026-06-10-multiscript-text-rendering.md.
_FONT_THAI: Final[Path] = (
    Path(__file__).resolve().parent.parent / "assets" / "fonts" / "NotoSansThai-Variable.ttf"
)
_FONT_JP: Final[Path] = (
    Path(__file__).resolve().parent.parent / "assets" / "fonts" / "NotoSansJP-Variable.ttf"
)
_FONT_HK: Final[Path] = (
    Path(__file__).resolve().parent.parent / "assets" / "fonts" / "NotoSansHK-Variable.ttf"
)
_FONT_KR: Final[Path] = (
    Path(__file__).resolve().parent.parent / "assets" / "fonts" / "NotoSansKR-Variable.ttf"
)
_FONT_SC: Final[Path] = (
    Path(__file__).resolve().parent.parent / "assets" / "fonts" / "NotoSansSC-Variable.ttf"
)
_FONT_DEVANAGARI: Final[Path] = (
    Path(__file__).resolve().parent.parent / "assets" / "fonts" / "NotoSansDevanagari-Variable.ttf"
)

# Distinctive high-frequency characters whose Simplified and Traditional
# forms differ. Han codepoints are otherwise shared (Han unification), so
# when a string has Han but no kana/hangul we count marker hits from each
# set to pick SC vs HK glyph shapes. Ambiguous all-shared strings default
# to Traditional — the Chinese market in the sheet today is Hong Kong.
_SIMPLIFIED_MARKERS: Final[frozenset[str]] = frozenset(
    "们这说对时会学国发见关问长门马车东买动业体让点战书读写电话语红钱价优广厂网头实现进选资讯"
)
_TRADITIONAL_MARKERS: Final[frozenset[str]] = frozenset(
    "們這說對時會學國發見關問長門馬車東買動業體讓點戰書讀寫電話語紅錢價優廣廠網頭實現進選資訊"
)


def _non_latin_script_for(text: str) -> str | None:
    """Detect the script of ``text`` when it needs a non-Inter font.

    Returns ``'hebrew' | 'arabic' | 'cyrillic' | 'thai' | 'jp' | 'ko' |
    'devanagari' | 'zh-hant' | 'zh-hans'`` or None for everything
    Latin-ish. RTL scripts, Cyrillic, Thai, Devanagari, kana and hangul
    return on first hit — they are unambiguous. Han only sets a flag while
    scanning: kana anywhere wins (Japanese headlines always carry some),
    hangul wins for Korean-with-Hanja, and Han alone is disambiguated
    Simplified vs Traditional via the marker sets above.

    Codepoint ranges:
      * Hebrew:     U+0590..05FF, presentation forms U+FB1D..FB4F
      * Arabic:     U+0600..06FF, supplement U+0750..077F
      * Cyrillic:   U+0400..04FF, supplement U+0500..052F
      * Thai:       U+0E00..0E7F
      * Devanagari: U+0900..097F
      * Kana:       U+3040..30FF, phonetic ext U+31F0..31FF,
                    halfwidth katakana U+FF66..FF9D
      * Hangul:     U+1100..11FF, compat jamo U+3130..318F,
                    syllables U+AC00..D7A3
      * Han:        U+3400..4DBF (Ext A), U+4E00..9FFF, compat U+F900..FAFF
    """
    has_han = False
    for ch in text or "":
        cp = ord(ch)
        if 0x0590 <= cp <= 0x05FF or 0xFB1D <= cp <= 0xFB4F:
            return "hebrew"
        if 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F:
            return "arabic"
        if 0x0400 <= cp <= 0x04FF or 0x0500 <= cp <= 0x052F:
            return "cyrillic"
        if 0x0900 <= cp <= 0x097F:
            return "devanagari"
        if 0x0E00 <= cp <= 0x0E7F:
            return "thai"
        if 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF or 0xFF66 <= cp <= 0xFF9D:
            return "jp"
        if 0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F or 0xAC00 <= cp <= 0xD7A3:
            return "ko"
        if 0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF:
            has_han = True
    if has_han:
        simp = sum(1 for ch in text if ch in _SIMPLIFIED_MARKERS)
        trad = sum(1 for ch in text if ch in _TRADITIONAL_MARKERS)
        return "zh-hans" if simp > trad else "zh-hant"
    return None


# Generic Bold routing table: script → (font path, variation axes pinned in
# the order each font declares them — verified per file on 2026-06-10:
# Heebo [wght] · Cairo [wght, slnt] · Noto Thai/Devanagari [wght, wdth] ·
# Noto JP/HK/KR/SC [wght]). Cyrillic is absent on purpose: Inter covers it.
_SCRIPT_FONTS_BOLD: Final[dict[str, tuple[Path, tuple[float, ...]]]] = {
    "hebrew":     (_T3_FONT_HEBREW,  (700.0,)),
    "arabic":     (_T3_FONT_ARABIC,  (700.0, 0.0)),
    "thai":       (_FONT_THAI,       (700.0, 100.0)),
    "devanagari": (_FONT_DEVANAGARI, (700.0, 100.0)),
    "jp":         (_FONT_JP,         (700.0,)),
    "ko":         (_FONT_KR,         (700.0,)),
    "zh-hant":    (_FONT_HK,         (700.0,)),
    "zh-hans":    (_FONT_SC,         (700.0,)),
}

# T3 display routing: same detection, heavier weights to match the Black
# look of the template. Latin (incl. Vietnamese) falls through to Anton.
_T3_FONTS_BY_SCRIPT: Final[dict[str, Path]] = {
    "hebrew":     _T3_FONT_HEBREW,
    "arabic":     _T3_FONT_ARABIC,
    "cyrillic":   _T3_FONT_CYRILLIC,
    "thai":       _FONT_THAI,
    "devanagari": _FONT_DEVANAGARI,
    "jp":         _FONT_JP,
    "ko":         _FONT_KR,
    "zh-hant":    _FONT_HK,
    "zh-hans":    _FONT_SC,
}

# Variation axes pin per T3 font, in each font's declared axis order (None =
# static single-weight font, skip variation). Cairo declares [wght, slnt] —
# the old single-value pin raised inside set_variation_by_axes and silently
# left it at Regular; the full tuple fixes that.
_T3_FONT_AXES: Final[dict[str, tuple[float, ...] | None]] = {
    str(_T3_FONT_LATIN):    None,              # Anton ships in one weight
    str(_T3_FONT_CYRILLIC): (700.0,),          # Oswald Bold (axis caps at 700)
    str(_T3_FONT_HEBREW):   (900.0,),          # Heebo Black
    str(_T3_FONT_ARABIC):   (900.0, 0.0),      # Cairo Black, no slant
    str(_FONT_THAI):        (900.0, 100.0),    # Noto Thai Black, normal width
    str(_FONT_DEVANAGARI):  (900.0, 100.0),    # Noto Devanagari Black
    str(_FONT_JP):          (900.0,),          # Noto JP Black
    str(_FONT_KR):          (900.0,),          # Noto KR Black
    str(_FONT_HK):          (900.0,),          # Noto HK Black
    str(_FONT_SC):          (900.0,),          # Noto SC Black
}


# ── Glyph-coverage safety net ───────────────────────────────────────────────


# (font path, text hash) pairs already checked, so the cheap-but-not-free
# pixel comparison runs once per unique (font, text) — not once per
# size-fit iteration. Cleared when it grows past a sane bound.
_GLYPH_CHECKED: set[tuple[str, int]] = set()


def _render_char_sample(font: ImageFont.ImageFont, s: str) -> bytes:
    img = Image.new("L", (160, 80), 0)
    ImageDraw.Draw(img).text((8, 8), s, font=font, fill=255)
    data = img.tobytes()
    img.close()
    return data


def _warn_if_missing_glyphs(font: ImageFont.ImageFont, text: str) -> list[int]:
    """Log loudly when ``font`` lacks a glyph for any char in ``text``.

    The safety net behind the script router: a script with no route (or a
    coverage hole in a routed font) shows up in the HF logs as a named
    ``font_missing_glyphs`` warning instead of silent tofu on a shipped
    creative. A char counts as missing when it renders identically to the
    font's .notdef box (U+0378 is permanently unassigned, so it always
    renders .notdef). Returns the missing codepoints (for tests).
    """
    path = getattr(font, "path", "")
    if not path or not text:
        return []
    key = (path, hash(text))
    if key in _GLYPH_CHECKED:
        return []
    if len(_GLYPH_CHECKED) > 512:
        _GLYPH_CHECKED.clear()
    _GLYPH_CHECKED.add(key)

    # Unique non-ASCII chars, capped to bound the pixel-compare cost.
    candidates: list[str] = []
    for ch in text:
        if ord(ch) > 0x7F and not ch.isspace() and ch not in candidates:
            candidates.append(ch)
        if len(candidates) >= 24:
            break
    if not candidates:
        return []

    notdef = _render_char_sample(font, "͸")
    missing = [
        ord(ch) for ch in candidates
        if _render_char_sample(font, ch) == notdef
    ]
    if missing:
        _log.warning(
            "font_missing_glyphs",
            font=os.path.basename(path),
            codepoints=["U+%04X" % cp for cp in missing[:8]],
            missing_count=len(missing),
            sample=text[:40],
        )
    return missing


def _pick_template_3_font_path(text: str) -> str:
    """Pick the T3 font whose glyph set matches the text's script.

    Routes through ``_non_latin_script_for``; Latin (incl. Vietnamese),
    Greek and empty text fall through to Anton.
    """
    script = _non_latin_script_for(text)
    if script is not None:
        return str(_T3_FONTS_BY_SCRIPT[script])
    return str(_T3_FONT_LATIN)


def _load_template_3_font(text: str, size: int) -> ImageFont.ImageFont:
    """Load the T3 display font matching ``text``'s script at ``size`` px.

    For variable fonts we pin the axes to the pre-selected Black-ish weight;
    for Anton (single weight) we skip the variation call. On any failure
    falls through to PIL's bitmap font so a bad font file never crashes the
    renderer.
    """
    path = _pick_template_3_font_path(text)
    try:
        font = ImageFont.truetype(path, size=size)
    except OSError as e:
        _log.warning("card_t3_font_load_failed", path=path, error=str(e))
        return ImageFont.load_default()
    axes = _T3_FONT_AXES.get(path)
    if axes is not None:
        try:
            font.set_variation_by_axes(list(axes))
        except (OSError, AttributeError):
            pass    # static font or older Pillow — already at the bundled weight
    _warn_if_missing_glyphs(font, text)
    return font


def _find_font_path(override: str | None = None) -> str | None:
    """Return the first usable TTF path, or None to trigger the bitmap fallback.

    Resolution order: explicit override → bundled Inter Variable (full Latin
    Ext, Cyrillic, Greek, Vietnamese) → legacy bundled Inter Bold (basic
    Latin only) → system fonts.
    """
    if override and os.path.isfile(override):
        return override
    if _BUNDLED_FONT_PATH.is_file():
        return str(_BUNDLED_FONT_PATH)
    if _LEGACY_BUNDLED_FONT_PATH.is_file():
        return str(_LEGACY_BUNDLED_FONT_PATH)
    for candidate in _SYSTEM_FONT_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def _load_font(
    size: int, override: str | None = None, *, text: str = ""
) -> ImageFont.ImageFont:
    """Load the Bold body font for ``text`` at ``size`` px.

    ``text`` (the string about to be drawn) drives per-script routing:
    Thai / Japanese / Korean / Chinese (Traditional + Simplified) /
    Devanagari / Hebrew / Arabic land on a bundled Noto / Heebo / Cairo
    font pinned to Bold, because Inter has no glyphs for any of them
    (chat 2026-06-10 tofu reports). Everything else stays on the bundled
    Inter exactly as before: opsz=14 (display-shape glyphs) + wght=700
    (Bold). An explicit ``override`` path wins over routing. Falls back
    to default-weight glyphs on non-variable fonts (no exception) and to
    PIL's bitmap font on missing files.
    """
    script = None if override else _non_latin_script_for(text)
    routed = _SCRIPT_FONTS_BOLD.get(script) if script else None
    if routed is not None:
        routed_path, axes = routed
        try:
            font = ImageFont.truetype(str(routed_path), size=size)
        except OSError as e:
            # Fall through to the Inter path below — tofu beats a crash.
            _log.warning(
                "script_font_load_failed", path=str(routed_path), error=str(e)
            )
        else:
            try:
                font.set_variation_by_axes(list(axes))
            except (OSError, AttributeError):
                pass
            _warn_if_missing_glyphs(font, text)
            return font

    path = _find_font_path(override)
    if path is None:
        _log.warning("card_font_fallback_default", size=size)
        return ImageFont.load_default()
    try:
        font = ImageFont.truetype(path, size=size)
    except OSError as e:
        _log.warning("card_font_load_failed", path=path, error=str(e))
        return ImageFont.load_default()

    # Variable fonts need the weight axis pinned to render Bold; static
    # fonts (e.g. the legacy Inter-Bold.ttf fallback) raise OSError on
    # set_variation_by_axes — swallow it, the static font is already Bold.
    try:
        font.set_variation_by_axes(list(_BUNDLED_FONT_AXES))
    except (OSError, AttributeError):
        pass
    _warn_if_missing_glyphs(font, text)
    return font


# ── Layout helpers ───────────────────────────────────────────────────────────


def _fit_image_cover(src: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Cover-crop ``src`` to exactly ``target_w × target_h`` preserving aspect."""
    src_w, src_h = src.size
    src_aspect = src_w / src_h
    target_aspect = target_w / target_h

    if src_aspect > target_aspect:
        # Source is wider — fit height, crop sides.
        new_h = target_h
        new_w = int(round(new_h * src_aspect))
    else:
        new_w = target_w
        new_h = int(round(new_w / src_aspect))

    resized = src.resize((new_w, new_h), Image.Resampling.LANCZOS)
    try:
        left = (new_w - target_w) // 2
        top = (new_h - target_h) // 2
        return resized.crop((left, top, left + target_w, top + target_h))
    finally:
        if resized is not src:
            resized.close()


def _text_width(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont
) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _break_long_word(
    draw: ImageDraw.ImageDraw,
    word: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    """Character-level greedy break for a word wider than ``max_width``.

    Thai, Japanese and Chinese write without spaces, so a whole sentence
    arrives as one "word" — without this it either shrinks to the floor
    font size or overflows the canvas. Each returned chunk fills its line,
    so the wrap loop never pairs two chunks of the same word with a fake
    space between them. (Proper Thai word segmentation needs a dictionary;
    char-level breaks are the accepted tradeoff — see the 2026-06-10 plan.)
    """
    chunks: list[str] = []
    current = ""
    for ch in word:
        if current and _text_width(draw, current + ch, font) > max_width:
            chunks.append(current)
            current = ch
        else:
            current += ch
    if current:
        chunks.append(current)
    return chunks or [word]


def _wrap_text_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    """Greedy word-wrap so each line fits within ``max_width`` px.

    Words that alone exceed ``max_width`` (spaceless scripts, pasted URLs)
    are pre-split at character level so they wrap instead of overflowing.
    """
    words: list[str] = []
    for word in (text or "").split():
        if _text_width(draw, word, font) <= max_width:
            words.append(word)
        else:
            words.extend(_break_long_word(draw, word, font, max_width))
    if not words:
        return []
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        if _text_width(draw, candidate, font) <= max_width or not current:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def _fit_pill_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    max_width: int,
    initial_size: int,
    min_size: int,
    font_override: str | None,
    font_loader: "Callable[[int], ImageFont.ImageFont] | None" = None,
) -> ImageFont.ImageFont:
    """Pick the largest font where ``text`` fits in ``max_width`` pixels.

    Used by the CTA pill so a long localised string (e.g. Polish
    "Dowiedz Się Więcej >>") shrinks down rather than overflowing the
    pill. Walks the font size DOWN from ``initial_size``; floors at
    ``min_size`` accepting overflow rather than degrading further.

    ``font_loader`` is an optional callable ``(size) -> ImageFont`` used by
    Template 3's script-aware loader. When None we fall back to the
    default loader, which routes by ``text``'s script (T1 / T2 / CTA pill
    default path).
    """
    loader = font_loader or (
        lambda s: _load_font(s, override=font_override, text=text)
    )
    size = initial_size
    while size >= min_size:
        font = loader(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        if text_w <= max_width:
            return font
        size -= max(2, size // 20)
    return loader(min_size)


def _fit_title_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_height: int,
    *,
    max_lines: int,
    initial_size: int,
    min_size: int,
    font_override: str | None,
    font_loader: "Callable[[int], ImageFont.ImageFont] | None" = None,
) -> tuple[ImageFont.ImageFont, list[str]]:
    """Pick the largest font size where the wrapped text fits the box.

    Walks the font size DOWN from ``initial_size`` until the wrapped block
    fits both width and height constraints; floors at ``min_size``. Returns
    the font and the wrapped lines so callers can draw without re-wrapping.

    ``font_loader`` lets a template plug in a non-default font (e.g.
    Template 3 uses ``_load_template_3_font`` to pick Anton / Oswald /
    Heebo / Cairo per script). The default loader routes by ``text``'s
    script.
    """
    loader = font_loader or (
        lambda s: _load_font(s, override=font_override, text=text)
    )
    size = initial_size
    while size >= min_size:
        font = loader(size)
        lines = _wrap_text_to_width(draw, text, font, max_width)
        if not lines:
            return font, []
        if len(lines) <= max_lines:
            # Measure total block height.
            line_heights = [
                draw.textbbox((0, 0), ln or " ", font=font)[3]
                - draw.textbbox((0, 0), ln or " ", font=font)[1]
                for ln in lines
            ]
            line_spacing = int(size * 0.18)
            total_h = sum(line_heights) + line_spacing * (len(lines) - 1)
            if total_h <= max_height:
                return font, lines
        size -= max(2, size // 20)
    # Floor: accept overflow at min_size rather than degrade further.
    font = loader(min_size)
    lines = _wrap_text_to_width(draw, text, font, max_width)
    return font, lines


def _vertical_gradient(
    width: int,
    height: int,
    top: tuple[int, int, int],
    bottom: tuple[int, int, int],
    *,
    top_alpha: int = 60,
    bottom_alpha: int = 240,
) -> Image.Image:
    """Build an RGBA gradient image (transparent top, opaque bottom).

    The alpha ramp keeps the upper portion of the canvas image visible while
    making the lower portion dark enough for white text to be legible.
    """
    grad = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = grad.load()
    if pixels is None:    # defensive; PIL always returns one
        return grad
    for y in range(height):
        t = y / max(1, height - 1)
        r = int(round(top[0] + (bottom[0] - top[0]) * t))
        g = int(round(top[1] + (bottom[1] - top[1]) * t))
        b = int(round(top[2] + (bottom[2] - top[2]) * t))
        a = int(round(top_alpha + (bottom_alpha - top_alpha) * t))
        for x in range(width):
            pixels[x, y] = (r, g, b, a)
    return grad


def _draw_pill(
    canvas: Image.Image,
    text: str,
    *,
    font: ImageFont.ImageFont,
    bg: tuple[int, int, int],
    text_color: tuple[int, int, int],
    center_x: int,
    center_y: int,
    pad_x: int,
    pad_y: int,
    max_width: int,
    min_width: int = 0,
) -> tuple[int, int]:
    """Draw a centered rounded-rectangle pill. Returns (pill_w, pill_h).

    ``min_width`` forces the pill to be at least that wide regardless of
    text length — used by Template 2 to render a full-width CTA pill that
    matches the user-supplied mockup. Default 0 = hugs the text + padding.
    """
    draw = ImageDraw.Draw(canvas)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    pill_w = min(max(text_w + pad_x * 2, min_width), max_width)
    pill_h = text_h + pad_y * 2
    left = center_x - pill_w // 2
    top = center_y - pill_h // 2
    right = left + pill_w
    bottom = top + pill_h
    radius = pill_h // 2
    draw.rounded_rectangle(
        (left, top, right, bottom), radius=radius, fill=bg
    )
    # Text baseline correction: textbbox y0 is non-zero for tall fonts.
    tx = center_x - text_w // 2 - bbox[0]
    ty = center_y - text_h // 2 - bbox[1]
    draw.text((tx, ty), text, fill=text_color, font=font)
    return pill_w, pill_h


# ── Template renderers ──────────────────────────────────────────────────────


def _render_template_1(
    background: Image.Image,
    headline: str,
    cta: str,
    width: int,
    height: int,
    font_override: str | None,
) -> Image.Image:
    """Image on top, white strip with purple title + pink CTA pill at bottom."""
    design = _DESIGNS[TEMPLATE_1]
    image_h = int(round(height * design.image_cover_frac))
    strip_h = height - image_h

    canvas = Image.new("RGB", (width, height), design.strip_bg or (255, 255, 255))

    # Cover-crop the source image into the upper region.
    fitted = _fit_image_cover(background, width, image_h)
    try:
        canvas.paste(fitted, (0, 0))
    finally:
        fitted.close()

    draw = ImageDraw.Draw(canvas)

    # Accent line directly under the image.
    if design.accent_line_color is not None:
        line_thickness = max(2, height // 360)
        draw.rectangle(
            (0, image_h, width, image_h + line_thickness),
            fill=design.accent_line_color,
        )

    # Title region inside the strip — leave room for the CTA below.
    side_padding = int(width * 0.04)
    title_top = image_h + int(strip_h * 0.10)
    cta_h_reserve = int(strip_h * 0.40)
    title_max_h = strip_h - cta_h_reserve - int(strip_h * 0.10)
    title_max_w = width - side_padding * 2

    font, lines = _fit_title_font(
        draw,
        headline or "",
        title_max_w,
        title_max_h,
        max_lines=2,
        initial_size=int(strip_h * 0.36),
        min_size=max(14, int(strip_h * 0.18)),
        font_override=font_override,
    )

    if lines:
        line_heights = [
            draw.textbbox((0, 0), ln or " ", font=font)[3]
            - draw.textbbox((0, 0), ln or " ", font=font)[1]
            for ln in lines
        ]
        line_spacing = int(font.size * 0.18) if hasattr(font, "size") else 4
        block_h = sum(line_heights) + line_spacing * (len(lines) - 1)
        y = title_top + (title_max_h - block_h) // 2
        for ln, lh in zip(lines, line_heights, strict=True):
            bbox = draw.textbbox((0, 0), ln, font=font)
            tw = bbox[2] - bbox[0]
            tx = (width - tw) // 2 - bbox[0]
            ty = y - bbox[1]
            draw.text((tx, ty), ln, fill=design.title_color, font=font)
            y += lh + line_spacing

    # CTA pill, centered horizontally near the bottom of the strip.
    # Pill font is capped at 60% of the title font so the title always
    # reads as the dominant element (Yoav 2026-06-08: "the CTA text is
    # bigger than the title text — it should be the other way around").
    # Pill auto-shrinks to fit when the localized CTA is long
    # (e.g. Polish "Dowiedz Się Więcej >>" overflowed at the fixed size).
    if cta:
        title_size_actual = getattr(font, "size", int(strip_h * 0.22))
        pill_max_width = int(width * 0.92)
        pill_pad_x = int(width * 0.04)
        pill_initial = max(14, int(title_size_actual * 0.60))
        pill_min = max(12, int(strip_h * 0.13))
        pill_font = _fit_pill_font(
            draw, cta,
            max_width=pill_max_width - pill_pad_x * 2,
            initial_size=pill_initial,
            min_size=pill_min,
            font_override=font_override,
        )
        cta_center_y = image_h + strip_h - int(strip_h * 0.22)
        _draw_pill(
            canvas,
            cta,
            font=pill_font,
            bg=design.cta_bg,
            text_color=design.cta_text_color,
            center_x=width // 2,
            center_y=cta_center_y,
            pad_x=pill_pad_x,
            pad_y=int(strip_h * 0.06),
            max_width=pill_max_width,
        )

    return canvas


def _render_template_2(
    background: Image.Image,
    headline: str,
    cta: str,
    width: int,
    height: int,
    font_override: str | None,
) -> Image.Image:
    """Image fills canvas; green gradient overlay; white title + yellow pill."""
    design = _DESIGNS[TEMPLATE_2]

    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    fitted = _fit_image_cover(background, width, height)
    try:
        canvas.paste(fitted, (0, 0))
    finally:
        fitted.close()

    # Gradient overlay — bright at top, opaque dark green at bottom — applied
    # over the full canvas so the image is gently tinted and the bottom text
    # area is dark enough for white text.
    assert design.gradient_top is not None and design.gradient_bottom is not None
    overlay = _vertical_gradient(
        width, height, design.gradient_top, design.gradient_bottom,
        top_alpha=40, bottom_alpha=235,
    )
    try:
        # Slight blur softens the gradient banding on small canvases.
        soft = overlay.filter(ImageFilter.GaussianBlur(radius=1))
        try:
            canvas_rgba = canvas.convert("RGBA")
            try:
                canvas_rgba.alpha_composite(soft)
                canvas = canvas_rgba.convert("RGB")
            finally:
                canvas_rgba.close()
        finally:
            soft.close()
    finally:
        overlay.close()

    draw = ImageDraw.Draw(canvas)

    # Layout (Yoav 2026-06-08): the CTA pill is anchored to the bottom and
    # spans (nearly) the full canvas width to match the user-supplied mockup;
    # the title block sits bottom-aligned RIGHT ABOVE the pill (no big gap).
    # We compute pill geometry first so the title can be positioned relative
    # to it, then draw both.
    strip_h = int(round(height * design.strip_height_frac))
    side_padding = int(width * 0.03)            # tight padding for near-full-width
    pill_bottom_margin = int(strip_h * 0.06)    # gap between pill and canvas bottom
    title_to_pill_gap = int(strip_h * 0.05)     # gap between title block and pill top

    # Pill geometry — pill font is sized AFTER the title so we can cap it
    # below the title font (Yoav 2026-06-08: title must read as the dominant
    # element). We use a placeholder size to measure pill height; the actual
    # pill font is computed below using the fitted title font's actual size.
    pill_pad_x = int(width * 0.04)
    pill_pad_y = int(strip_h * 0.06)
    pill_full_width = int(width * 0.94)
    # Placeholder height — recomputed once we have the real pill font.
    placeholder_pill_font = _load_font(
        max(14, int(strip_h * 0.18)), override=font_override, text=cta or ""
    )
    placeholder_bbox = draw.textbbox((0, 0), cta or "X", font=placeholder_pill_font)
    placeholder_text_h = placeholder_bbox[3] - placeholder_bbox[1]
    pill_h_actual = placeholder_text_h + pill_pad_y * 2
    pill_center_y = height - pill_bottom_margin - pill_h_actual // 2
    pill_top = pill_center_y - pill_h_actual // 2

    # Title region: from where the strip starts down to just above the pill.
    title_max_w = width - side_padding * 2
    title_top_bound = height - strip_h + int(strip_h * 0.04)
    title_bottom_bound = pill_top - title_to_pill_gap
    title_max_h = max(int(strip_h * 0.20), title_bottom_bound - title_top_bound)

    font, lines = _fit_title_font(
        draw,
        headline or "",
        title_max_w,
        title_max_h,
        max_lines=2,
        initial_size=int(strip_h * 0.32),
        min_size=max(14, int(strip_h * 0.16)),
        font_override=font_override,
    )

    if lines:
        line_heights = [
            draw.textbbox((0, 0), ln or " ", font=font)[3]
            - draw.textbbox((0, 0), ln or " ", font=font)[1]
            for ln in lines
        ]
        line_spacing = int(font.size * 0.16) if hasattr(font, "size") else 4
        block_h = sum(line_heights) + line_spacing * (len(lines) - 1)
        # Bottom-align the title block to sit right above the pill.
        y = title_bottom_bound - block_h
        for ln, lh in zip(lines, line_heights, strict=True):
            bbox = draw.textbbox((0, 0), ln, font=font)
            tw = bbox[2] - bbox[0]
            tx = (width - tw) // 2 - bbox[0]
            ty = y - bbox[1]
            draw.text((tx, ty), ln, fill=design.title_color, font=font)
            y += lh + line_spacing

    if cta:
        title_size_actual = getattr(font, "size", int(strip_h * 0.28))
        pill_initial = max(14, int(title_size_actual * 0.60))
        pill_min = max(12, int(strip_h * 0.12))
        pill_font = _fit_pill_font(
            draw, cta,
            max_width=pill_full_width - pill_pad_x * 2,
            initial_size=pill_initial,
            min_size=pill_min,
            font_override=font_override,
        )
        _draw_pill(
            canvas,
            cta,
            font=pill_font,
            bg=design.cta_bg,
            text_color=design.cta_text_color,
            center_x=width // 2,
            center_y=pill_center_y,
            pad_x=pill_pad_x,
            pad_y=pill_pad_y,
            max_width=pill_full_width,
            min_width=pill_full_width,    # force full width (matches mockup)
        )

    return canvas


def _render_template_3(
    background: Image.Image,
    headline: str,
    cta: str,
    width: int,
    height: int,
    font_override: str | None,
) -> Image.Image:
    """Image cover-crops top 75%; thin red separator; deep-navy band with
    white headline; bright-red full-width pill with yellow CTA at the
    very bottom. Mockup: user-supplied 2026-06-08."""
    design = _DESIGNS[TEMPLATE_3]

    # Split the 25% non-image region into the navy band (68% of that) and
    # the red pill (32%). Pill ends flush with the canvas bottom; band
    # ends flush with the pill top — no gap between them, matching the
    # mockup.
    strip_h = int(round(height * design.strip_height_frac))
    pill_region_h = int(round(strip_h * 0.32))
    band_h = strip_h - pill_region_h
    image_h = height - strip_h

    canvas = Image.new("RGB", (width, height), design.strip_bg or (0, 0, 0))

    # Cover-crop the source image into the upper region.
    fitted = _fit_image_cover(background, width, image_h)
    try:
        canvas.paste(fitted, (0, 0))
    finally:
        fitted.close()

    draw = ImageDraw.Draw(canvas)

    # Thin red separator directly under the image. The line height scales
    # with canvas height so it's visible at 720p and not garish at 4K.
    # Order matters: draw the navy band FIRST (starting BELOW the line),
    # then draw the line on top so it sits flush against both the image
    # above and the navy band below. (Earlier ordering had the navy
    # rectangle painting over the red line — invisible separator bug.)
    line_thickness = max(3, height // 240) if design.accent_line_color else 0
    band_top = image_h + line_thickness
    band_bottom = image_h + band_h
    draw.rectangle(
        (0, band_top, width, band_bottom), fill=design.strip_bg or (15, 30, 55)
    )
    if design.accent_line_color is not None:
        draw.rectangle(
            (0, image_h, width, image_h + line_thickness),
            fill=design.accent_line_color,
        )

    # Title region: centered inside the navy band with side padding.
    side_padding = int(width * 0.04)
    title_max_w = width - side_padding * 2
    title_top_bound = band_top + int(band_h * 0.10)
    title_bottom_bound = band_bottom - int(band_h * 0.10)
    title_max_h = max(int(band_h * 0.50), title_bottom_bound - title_top_bound)

    headline_text = (headline or "").upper()    # mockup uses all-caps headlines
    # T3 uses a heavy condensed display font (Anton for Latin, Oswald for
    # Cyrillic, Heebo for Hebrew, Cairo for Arabic) instead of Inter — see
    # ``_load_template_3_font`` for the per-script picker. The font_loader
    # closure freezes the headline so every size-fit attempt picks the
    # matching font.
    title_loader = lambda s: _load_template_3_font(headline_text, s)    # noqa: E731
    font, lines = _fit_title_font(
        draw,
        headline_text,
        title_max_w,
        title_max_h,
        max_lines=2,
        initial_size=int(band_h * 0.42),
        min_size=max(14, int(band_h * 0.22)),
        font_override=font_override,
        font_loader=title_loader,
    )

    if lines:
        line_heights = [
            draw.textbbox((0, 0), ln or " ", font=font)[3]
            - draw.textbbox((0, 0), ln or " ", font=font)[1]
            for ln in lines
        ]
        line_spacing = int(font.size * 0.18) if hasattr(font, "size") else 4
        block_h = sum(line_heights) + line_spacing * (len(lines) - 1)
        # Vertically center the title block inside the navy band.
        y = title_top_bound + (title_max_h - block_h) // 2
        for ln, lh in zip(lines, line_heights, strict=True):
            bbox = draw.textbbox((0, 0), ln, font=font)
            tw = bbox[2] - bbox[0]
            tx = (width - tw) // 2 - bbox[0]
            ty = y - bbox[1]
            draw.text((tx, ty), ln, fill=design.title_color, font=font)
            y += lh + line_spacing

    # Red full-width pill at the bottom — touches the navy band, ends
    # flush with the canvas bottom. Pill text auto-shrinks for long
    # localized CTAs (e.g. Polish "Dowiedz Się Więcej >>"), capped at
    # 60% of the title font size so the title still reads as dominant.
    if cta:
        pill_left = 0
        pill_right = width
        pill_top = band_bottom
        pill_bottom = height
        # Square corners on the pill match the mockup (a clean band, not
        # a rounded button) — implemented as a flat rectangle with
        # text centered inside.
        draw.rectangle(
            (pill_left, pill_top, pill_right, pill_bottom), fill=design.cta_bg
        )

        pill_pad_x = int(width * 0.04)
        title_size_actual = getattr(font, "size", int(band_h * 0.36))
        pill_initial = max(14, int(title_size_actual * 0.60))
        pill_min = max(12, int(pill_region_h * 0.30))
        # Reuse the script-aware loader for the CTA pill too — keeps the
        # title and CTA visually consistent (the mockup uses the same
        # display font for both) and makes a Polish CTA like
        # "DOWIEDZ SIĘ WIĘCEJ >>" render in Anton, a Hebrew CTA render
        # in Heebo, etc.
        pill_loader = lambda s: _load_template_3_font(cta, s)    # noqa: E731
        pill_font = _fit_pill_font(
            draw, cta,
            max_width=width - pill_pad_x * 2,
            initial_size=pill_initial,
            min_size=pill_min,
            font_override=font_override,
            font_loader=pill_loader,
        )
        bbox = draw.textbbox((0, 0), cta, font=pill_font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        tx = (width - text_w) // 2 - bbox[0]
        ty = pill_top + (pill_region_h - text_h) // 2 - bbox[1]
        draw.text((tx, ty), cta, fill=design.cta_text_color, font=pill_font)

    return canvas


# ── Public entry ─────────────────────────────────────────────────────────────


_DISPATCH = {
    TEMPLATE_1: _render_template_1,
    TEMPLATE_2: _render_template_2,
    TEMPLATE_3: _render_template_3,
}


def render_card_bytes(
    *,
    template_id: str,
    background_image_bytes: bytes,
    headline: str,
    cta: str,
    width: int,
    height: int,
    font_override: str | None = None,
) -> bytes:
    """Render a finished card PNG. Returns bytes ready for upload.

    Raises ``ValueError`` for an unknown template id. Empty ``headline`` is
    allowed (no title drawn). Empty ``cta`` is allowed (no pill drawn — used
    when both the row cell and the per-template default setting are blank).
    """
    if template_id not in SUPPORTED_TEMPLATES:
        raise ValueError(
            f"unknown card template id: {template_id!r}; "
            f"expected one of {sorted(SUPPORTED_TEMPLATES)}"
        )
    if width <= 0 or height <= 0:
        raise ValueError(f"width and height must be positive (got {width}x{height})")

    _log.info(
        "card_render_start",
        template=template_id,
        width=width,
        height=height,
        headline_chars=len(headline or ""),
        cta_chars=len(cta or ""),
    )

    with Image.open(io.BytesIO(background_image_bytes)) as src:
        src.load()
        bg = src.convert("RGB")

    try:
        renderer = _DISPATCH[template_id]
        result = renderer(bg, headline or "", cta or "", width, height, font_override)
        try:
            out = io.BytesIO()
            result.save(out, format="PNG", optimize=True)
            data = out.getvalue()
        finally:
            if result is not bg:
                result.close()
    finally:
        bg.close()

    _log.info(
        "card_render_done",
        template=template_id,
        width=width,
        height=height,
        bytes_out=len(data),
    )
    return data
