"""Tests for per-script font routing and spaceless-script wrapping.

The 2026-06-10 tofu reports (Thai, Hong Kong Chinese, Japanese, Hebrew
screenshots from the bulk team) traced to every generic text paint going
through bundled Inter, which has no glyphs for those scripts. Routing now
lives in ``card_renderer`` and is shared by the card templates, the
text_on_img overlay and the cartoon CTA pill.

Plan: ``_plans/2026-06-10-multiscript-text-rendering.md``.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from bulkvid.pipeline.card_renderer import (
    _FONT_HK,
    _FONT_JP,
    _FONT_THAI,
    _load_font,
    _non_latin_script_for,
    _pick_template_3_font_path,
    _wrap_text_to_width,
)

# Real strings from the production sheet (chat screenshots 2026-06-10).
THAI = "ไทย ฟันเทียมทั้งปากและฟันติดแน่นราคาและข้อมูล"
JAPANESE = "官公庁オークション:差押車・未使用車をお得に入手する方法"
CHINESE_HK = "香港長者醫保:選擇與資訊"
HEBREW = "מחירי מכשירי שמיעה לקשישים בישראל"
LATIN = "Schraubenlose Zahnimplantate: Kosten & Info"


# ── Script detection ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (THAI, "thai"),
        (JAPANESE, "jp"),
        (CHINESE_HK, "zh"),
        (HEBREW, "hebrew"),
        ("سيارات مستعملة للبيع", "arabic"),
        ("Подержанные автомобили", "cyrillic"),
        (LATIN, None),
        ("Cache-tétons sophistiqués, sans soutien-gorge", None),    # Latin Ext
        ("", None),
    ],
)
def test_non_latin_script_detection(text: str, expected: str | None) -> None:
    assert _non_latin_script_for(text) == expected


def test_han_with_kana_anywhere_reads_as_japanese() -> None:
    """Han chars come first here; the katakana later in the string must
    still flip the whole text to Japanese (kana wins over Han)."""
    assert _non_latin_script_for("中古車オークション") == "jp"


def test_han_without_kana_reads_as_chinese() -> None:
    assert _non_latin_script_for("香港長者醫保") == "zh"


# ── Generic font routing (_load_font) ────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected_path"),
    [
        (THAI, _FONT_THAI),
        (JAPANESE, _FONT_JP),
        (CHINESE_HK, _FONT_HK),
    ],
)
def test_load_font_routes_to_script_font(text: str, expected_path) -> None:
    font = _load_font(48, text=text)
    assert font.path == str(expected_path)


def test_load_font_keeps_inter_for_latin() -> None:
    font = _load_font(48, text=LATIN)
    assert "Inter" in font.path


def test_load_font_hebrew_routes_to_heebo() -> None:
    font = _load_font(48, text=HEBREW)
    assert "Heebo" in font.path


def test_load_font_override_wins_over_routing() -> None:
    """An explicit override path must keep today's semantics even when the
    text is non-Latin."""
    inter = _load_font(48, text=LATIN)
    font = _load_font(48, override=inter.path, text=THAI)
    assert font.path == inter.path


# ── Tofu regression ──────────────────────────────────────────────────────────


def _render_bytes(font, text: str) -> bytes:
    img = Image.new("L", (600, 160), 0)
    ImageDraw.Draw(img).text((10, 10), text, font=font, fill=255)
    data = img.tobytes()
    img.close()
    return data


@pytest.mark.parametrize(
    "text", [THAI, JAPANESE, CHINESE_HK, HEBREW], ids=["th", "jp", "zh", "he"]
)
def test_routed_font_has_real_glyphs(text: str) -> None:
    """Render through the routed font and compare against the same font's
    .notdef boxes (U+0378 is permanently unassigned). Identical bitmaps
    would mean the script still renders as tofu."""
    font = _load_font(64, text=text)
    sample = text.replace(" ", "")[:4]
    notdef = "͸" * len(sample)
    assert _render_bytes(font, sample) != _render_bytes(font, notdef)


# ── Template 3 routing ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected_path"),
    [
        (THAI, _FONT_THAI),
        (JAPANESE, _FONT_JP),
        (CHINESE_HK, _FONT_HK),
    ],
)
def test_template_3_picker_covers_new_scripts(text: str, expected_path) -> None:
    assert _pick_template_3_font_path(text) == str(expected_path)


def test_template_3_picker_keeps_anton_for_latin() -> None:
    assert "Anton" in _pick_template_3_font_path(LATIN)


# ── Spaceless-script wrapping ────────────────────────────────────────────────


def _draw() -> ImageDraw.ImageDraw:
    return ImageDraw.Draw(Image.new("RGB", (10, 10)))


@pytest.mark.parametrize(
    "text",
    [THAI.replace(" ", ""), JAPANESE, CHINESE_HK.replace(" ", "")],
    ids=["th", "jp", "zh"],
)
def test_wrap_breaks_spaceless_scripts_to_fit(text: str) -> None:
    """A spaceless sentence must wrap into multiple lines that each fit
    ``max_width`` instead of shipping one overflowing line."""
    draw = _draw()
    font = _load_font(48, text=text)
    max_width = 400
    lines = _wrap_text_to_width(draw, text, font, max_width)
    assert len(lines) > 1
    for ln in lines:
        bbox = draw.textbbox((0, 0), ln, font=font)
        assert bbox[2] - bbox[0] <= max_width, ln
    # Nothing dropped and no fake spaces injected mid-script.
    assert "".join(lines) == text


def test_wrap_latin_behavior_unchanged() -> None:
    draw = _draw()
    font = _load_font(32, text=LATIN)
    lines = _wrap_text_to_width(draw, LATIN, font, 300)
    assert " ".join(lines) == LATIN
