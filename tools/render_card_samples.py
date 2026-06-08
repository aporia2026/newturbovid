"""Render sample card outputs for visual review.

Run from the repo root: ``python tools/render_card_samples.py``. Drops 4 PNGs
into ``apps_script/template_previews/_samples/`` (gitignored output dir).

Used during initial development of the card renderer to eyeball the output
against the user-supplied mockups (``template_1.png`` / ``template_2.png``).
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from bulkvid.pipeline.card_renderer import (    # noqa: E402
    TEMPLATE_1,
    TEMPLATE_2,
    TEMPLATE_3,
    render_card_bytes,
)


def _make_placeholder_photo(width: int = 1200, height: int = 1200) -> bytes:
    """Synthesize a photo-like gradient so the operator can see what a real
    kie-generated image would look like inside the card."""
    img = Image.new("RGB", (width, height), (30, 100, 200))
    draw = ImageDraw.Draw(img)
    # Sky -> ground gradient.
    for y in range(height):
        t = y / (height - 1)
        r = int(60 + (210 - 60) * t)
        g = int(140 + (190 - 140) * t)
        b = int(220 + (130 - 220) * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    # A couple of "subject" shapes so it isn't a flat gradient.
    draw.ellipse((width * 0.55, height * 0.10, width * 0.85, height * 0.30), fill=(255, 220, 100))
    draw.rectangle((width * 0.05, height * 0.60, width * 0.45, height * 0.85), fill=(70, 50, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img.close()
    return buf.getvalue()


def main() -> None:
    out_dir = REPO_ROOT / "apps_script" / "template_previews" / "_samples"
    out_dir.mkdir(parents=True, exist_ok=True)

    bg = _make_placeholder_photo()
    headline = "Leadership Féminin Durable Mode Intime & Digital 2025"

    cases = [
        (TEMPLATE_1, "DISCOVER MORE >>", 1080, 1080, "template_1_1x1.png"),
        (TEMPLATE_1, "DISCOVER MORE >>", 1080, 1920, "template_1_9x16.png"),
        (TEMPLATE_2, "See The Full Guide >>", 1080, 1080, "template_2_1x1.png"),
        (TEMPLATE_2, "See The Full Guide >>", 1080, 1920, "template_2_9x16.png"),
        (TEMPLATE_3, "DISCOVER MORE >>", 1080, 1080, "template_3_1x1.png"),
        (TEMPLATE_3, "DISCOVER MORE >>", 1080, 1920, "template_3_9x16.png"),
    ]

    for template_id, cta, w, h, filename in cases:
        data = render_card_bytes(
            template_id=template_id,
            background_image_bytes=bg,
            headline=headline,
            cta=cta,
            width=w,
            height=h,
        )
        path = out_dir / filename
        path.write_bytes(data)
        print(f"wrote {path.relative_to(REPO_ROOT)} ({len(data) // 1024} KB)")


if __name__ == "__main__":
    main()
