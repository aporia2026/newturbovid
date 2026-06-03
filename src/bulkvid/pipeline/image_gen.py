"""Image generation with kie.ai primary, AtlasCloud fallback.

Wraps the two image-gen adapters into one function that the row processor
calls. If kie.ai fails for any reason (rate limit, task failure, timeout,
auth, all keys cooled down), we transparently fall back to AtlasCloud
when configured. The caller never sees the fallback — they get back
``(url, cost)`` like before.

The fallback is opt-out via ``atlas`` being ``None``. When kie.ai succeeds,
AtlasCloud is never invoked.

Plan §5 (Image generation), §6 (Alternatives rejected — keep both vendors).
"""

from __future__ import annotations

from bulkvid.adapters.atlascloud import (
    AtlasCloudClient,
    AtlasError,
)
from bulkvid.adapters.kie import (
    KieClient,
    KieError,
    gpt_image_2,
    nano_banana_2,
)
from bulkvid.logging import get_logger

_log = get_logger("imagegen")


async def edit_with_fallback(
    *,
    kie: KieClient,
    atlas: AtlasCloudClient | None,
    source_image_url: str,
    prompt: str,
    aspect_ratio: str,
    resolution: str = "2K",
) -> tuple[str, float]:
    """Generate the 2x2 collage. Primary: Nano Banana 2. Fallback: GPT Image 2.

    Both run through kie.ai. Nano Banana 2 honors ``aspect_ratio`` and renders
    legible marketing text; GPT Image 2 (image-to-image) is the fallback for
    the same reasons. AtlasCloud stays as a last resort when configured.
    Returns ``(url, cost_usd)``. Raises if every backend fails.
    """
    try:
        return await nano_banana_2(
            kie,
            source_image_url=source_image_url,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
        )
    except KieError as nb2_err:
        _log.warning("nano_banana_2_failed_falling_back", error=str(nb2_err)[:200])

    try:
        url, cost = await gpt_image_2(
            kie,
            source_image_url=source_image_url,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
        )
        _log.info("gpt_image_2_fallback_used", source="nano_banana_2_failure")
        return url, cost
    except KieError as gpt_err:
        if atlas is None:
            raise
        _log.warning("gpt_image_2_failed_falling_back_to_atlas", error=str(gpt_err)[:200])
        try:
            url, cost = await atlas.edit_image(
                source_image_url=source_image_url,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )
            _log.info("atlas_fallback_used", source="gpt_image_2_failure")
            return url, cost
        except AtlasError as atlas_err:
            raise KieError(
                f"All image backends failed. "
                f"nano-banana-2 + gpt-image-2 (kie) and AtlasCloud. "
                f"gpt-image-2={gpt_err!s} | atlas={atlas_err!s}"
            ) from atlas_err
