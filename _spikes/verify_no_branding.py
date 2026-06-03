"""Verify the NO_BRANDING fix: generate one car-heavy cartoon scene and check
that the image model produced no real logo/badge/plate. ~$0.04."""

from __future__ import annotations

import asyncio

from bulkvid.adapters.kie import build_client_from_settings, nano_banana_2_text_to_image
from bulkvid.pipeline.cartoon_prompt import image_prompt_for_shot

# Worst case for brand leakage: a person inspecting a car (the scene that leaked a VW badge).
SCENE = (
    "A cheerful young woman holding a magnifying glass and inspecting a parked "
    "compact car in a dealership lot, looking at the rear of the car."
)


async def main() -> None:
    kie = build_client_from_settings()
    prompt = image_prompt_for_shot(SCENE, is_chained=False)
    print("PROMPT:\n", prompt, "\n")
    try:
        url, cost = await nano_banana_2_text_to_image(kie, prompt, "9:16", resolution="1K")
        print("image url:", url)
        print("cost     :", cost)
    finally:
        await kie.aclose()


if __name__ == "__main__":
    asyncio.run(main())
