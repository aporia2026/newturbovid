"""Generate `template_{1,2}_labeled.png` from the source mockups.

Adds a "TEMPLATE 1" / "TEMPLATE 2" caption band to the top of each source
PNG so the in-sheet preview row is self-identifying — no separate label
cells needed.

Run after editing the source PNGs at
``apps_script/template_previews/template_{1,2}.png``:

    python tools/render_labeled_template_previews.py

Commits the regenerated `_labeled.png` files alongside the sources;
HuggingFace's resolver serves them via the URLs hard-coded in
``apps_script/Code.gs::CARD_TEMPLATE_PREVIEW_URLS``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "apps_script" / "template_previews"
# Variable Inter (full Latin Ext + Cyrillic + Greek + Vietnamese coverage).
# Same font the card_renderer uses — keep them in sync so the "TEMPLATE 1"
# preview label and the in-video Pillow overlay share a typeface.
FONT_PATH = REPO_ROOT / "src" / "bulkvid" / "assets" / "fonts" / "Inter-Variable.ttf"
_FONT_AXES = [14.0, 700.0]    # opsz=14 (display), wght=700 (Bold)

# Caption band sits at the top of the labeled image.
LABEL_BAND_HEIGHT_FRAC = 0.14          # ~14% of source height
LABEL_BG = (255, 255, 255)
LABEL_FG = (40, 40, 40)


def _label_image(n: int) -> None:
    src_path = SRC_DIR / f"template_{n}.png"
    out_path = SRC_DIR / f"template_{n}_labeled.png"
    if not src_path.is_file():
        print(f"ERROR: missing {src_path}", file=sys.stderr)
        sys.exit(2)

    with Image.open(src_path) as src:
        src_rgb = src.convert("RGB")
        src_w, src_h = src_rgb.size

        label_h = int(round(src_h * LABEL_BAND_HEIGHT_FRAC))
        out_h = src_h + label_h
        out = Image.new("RGB", (src_w, out_h), LABEL_BG)

        # White label band at top.
        draw = ImageDraw.Draw(out)
        font_size = int(label_h * 0.55)
        font = ImageFont.truetype(str(FONT_PATH), font_size)
        try:
            font.set_variation_by_axes(_FONT_AXES)
        except (OSError, AttributeError):
            pass    # static fallback font — already at the right weight
        text = f"TEMPLATE {n}"
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = (src_w - tw) // 2 - bbox[0]
        ty = (label_h - th) // 2 - bbox[1]
        draw.text((tx, ty), text, fill=LABEL_FG, font=font)

        # Paste the original template design below the band.
        out.paste(src_rgb, (0, label_h))

        out.save(out_path, "PNG", optimize=True)
        print(f"wrote {out_path.relative_to(REPO_ROOT)} ({out.size[0]}x{out.size[1]})")


def main() -> None:
    if not FONT_PATH.is_file():
        print(f"ERROR: bundled font missing at {FONT_PATH}", file=sys.stderr)
        sys.exit(3)
    for n in (1, 2):
        _label_image(n)


if __name__ == "__main__":
    main()
