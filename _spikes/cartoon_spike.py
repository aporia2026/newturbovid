"""Throwaway spike: validate the cartoon-mode LOOK before building the pipeline.

NOT production code. Lives in _spikes/, touches nothing in src/. It reuses the
existing KieClient / RendiClient and calls the two NEW model params directly
(nano-banana-2 text-to-image, Seedance 1.5 Pro image-to-video) so we can eyeball:

  1. Does nano-banana-2 produce the target warm flat-cartoon style from text?
  2. Does image-to-image chaining (shot 2 conditioned on shot 1) hold the same
     character / palette across the cut?
  3. Does a 4s Seedance animation of a still cartoon look good?
  4. Stitched together, does the cut read as one scene or two unrelated drawings?

Run (from project root, with .venv active):
    python _spikes/cartoon_spike.py

Prints every intermediate URL and writes the final stitched MP4 URL at the end.
Estimated spend: ~$0.15 (2 images + 2 short clips + 1 concat).
"""

from __future__ import annotations

import asyncio

from bulkvid.adapters.kie import build_client_from_settings as build_kie
from bulkvid.adapters.rendi import (
    build_client_from_settings as build_rendi,
    dimensions_for_ratio,
)

# ── Look spec ────────────────────────────────────────────────────────────────
# Generic / symbolic character only (no real, named person) — the editorial rule.
# Style preamble matches the reference frames the user supplied (warm, flat,
# semi-realistic digital cartoon, automotive/car-buying vertical).

STYLE = (
    "Flat semi-realistic digital cartoon illustration, warm soft lighting, "
    "clean confident linework, gentle painterly shading, vibrant but natural "
    "colors, modern 2D animated-film look."
)

ASPECT = "9:16"
IMAGE_RES = "1K"      # plenty; the clip renders at 720p
VIDEO_RES = "720p"
DURATION = 4          # Seedance minimum

SHOT1_IMAGE_PROMPT = (
    f"{STYLE} A cheerful young woman with long light-brown hair, wearing a green "
    f"knit sweater, sitting at a bright kitchen table looking at a laptop showing "
    f"car listings, a coffee mug beside her, soft morning light. Vertical "
    f"composition, head and shoulders to mid-torso framing."
)

SHOT2_IMAGE_PROMPT = (
    f"{STYLE} The SAME young woman — same face, same long light-brown hair, same "
    f"green knit sweater — now smiling confidently while driving a modern car, "
    f"both hands on the steering wheel, looking ahead through the windshield, "
    f"sunny street visible through the windows. Keep her appearance identical to "
    f"the reference image. Vertical composition."
)

SHOT1_MOTION = (
    "Subtle gentle motion: she scrolls the laptop and glances up thoughtfully, "
    "faint steam rising from the mug, soft ambient movement. Fixed, steady camera."
)

SHOT2_MOTION = (
    "She drives happily with a slight smile and small head turn, scenery drifting "
    "past the side windows, very subtle slow camera push-in."
)

MODEL_SEEDANCE = "bytedance/seedance-1.5-pro"
MODEL_NANO_BANANA_2 = "nano-banana-2"


async def _nano_banana_2_t2i(kie, prompt: str) -> str:
    """Text-to-image (NO seed). Returns image URL."""
    task = await kie.create_task(
        MODEL_NANO_BANANA_2,
        {
            "prompt": prompt,
            "aspect_ratio": ASPECT,
            "resolution": IMAGE_RES,
            "output_format": "png",
        },
    )
    urls = await kie.poll_task(task, max_attempts=60, delay_seconds=5.0)
    return urls[0]


async def _nano_banana_2_i2i(kie, prompt: str, source_url: str) -> str:
    """Image-to-image conditioned on source_url (consistency lever). Returns image URL."""
    task = await kie.create_task(
        MODEL_NANO_BANANA_2,
        {
            "prompt": prompt,
            "image_input": [source_url],
            "aspect_ratio": ASPECT,
            "resolution": IMAGE_RES,
            "output_format": "png",
        },
    )
    urls = await kie.poll_task(task, max_attempts=60, delay_seconds=5.0)
    return urls[0]


async def _seedance_i2v(kie, image_url: str, motion_prompt: str) -> str:
    """Animate one still image. Returns video (mp4) URL."""
    task = await kie.create_task(
        MODEL_SEEDANCE,
        {
            "prompt": motion_prompt,
            "input_urls": [image_url],
            "aspect_ratio": ASPECT,
            "resolution": VIDEO_RES,
            "duration": str(DURATION),
        },
    )
    # Video gen is slower than image gen — give it up to ~10 min.
    urls = await kie.poll_task(task, max_attempts=120, delay_seconds=5.0)
    return urls[0]


async def _concat(rendi, clip1_url: str, clip2_url: str) -> str:
    """Concat two clips into one, both normalized to the target aspect. Returns URL."""
    w, h = dimensions_for_ratio(ASPECT)
    cmd = (
        "-i {{in_1}} -i {{in_2}} "
        f'-filter_complex "[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,'
        f"crop={w}:{h},setsar=1[v0];"
        f"[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f'crop={w}:{h},setsar=1[v1];[v0][v1]concat=n=2:v=1:a=0[outv]" '
        '-map "[outv]" -c:v libx264 -pix_fmt yuv420p {{out_1}}'
    )
    cmd_id = await rendi.submit(
        cmd,
        {"in_1": clip1_url, "in_2": clip2_url},
        {"out_1": "cartoon_spike.mp4"},
    )
    return await rendi.poll(cmd_id, max_attempts=120, delay_seconds=5.0)


# Set both to a freshly-generated image URL to skip image gen and re-spend on a
# re-run; leave as None to generate from scratch.
SHOT1_IMG_OVERRIDE: str | None = None
SHOT2_IMG_OVERRIDE: str | None = None


async def main() -> None:
    kie = build_kie()
    rendi = build_rendi()
    try:
        if SHOT1_IMG_OVERRIDE and SHOT2_IMG_OVERRIDE:
            shot1_img, shot2_img = SHOT1_IMG_OVERRIDE, SHOT2_IMG_OVERRIDE
            print("[1-2/5] reusing existing shot images:", shot1_img, shot2_img)
        else:
            print("[1/5] nano-banana-2 text-to-image (shot 1)...")
            shot1_img = await _nano_banana_2_t2i(kie, SHOT1_IMAGE_PROMPT)
            print("      shot1 image:", shot1_img)

            print("[2/5] nano-banana-2 image-to-image (shot 2, chained on shot 1)...")
            shot2_img = await _nano_banana_2_i2i(kie, SHOT2_IMAGE_PROMPT, shot1_img)
            print("      shot2 image:", shot2_img)

        print("[3/5] Seedance animate shot 1...")
        clip1 = await _seedance_i2v(kie, shot1_img, SHOT1_MOTION)
        print("      clip1:", clip1)

        print("[4/5] Seedance animate shot 2...")
        clip2 = await _seedance_i2v(kie, shot2_img, SHOT2_MOTION)
        print("      clip2:", clip2)

        print("[5/5] Rendi concat...")
        final_url = await _concat(rendi, clip1, clip2)

        print("\n=== SPIKE RESULT ===")
        print("shot1 image :", shot1_img)
        print("shot2 image :", shot2_img)
        print("clip1       :", clip1)
        print("clip2       :", clip2)
        print("STITCHED    :", final_url)
    finally:
        await kie.aclose()
        await rendi.aclose()


if __name__ == "__main__":
    asyncio.run(main())
