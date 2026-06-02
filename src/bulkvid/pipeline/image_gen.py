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
    nano_banana_edit,
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
) -> tuple[str, float]:
    """Generate (or edit) an image. Try kie.ai first; on failure, AtlasCloud.

    Returns ``(url, cost_usd)``. Raises if both fail.
    """
    try:
        url, cost = await nano_banana_edit(
            kie,
            source_image_url=source_image_url,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
        )
        return url, cost
    except KieError as kie_err:
        if atlas is None:
            raise
        _log.warning(
            "kie_failed_falling_back_to_atlas",
            error=str(kie_err)[:200],
        )
        try:
            url, cost = await atlas.edit_image(
                source_image_url=source_image_url,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )
            _log.info("atlas_fallback_used", source="kie_failure")
            return url, cost
        except AtlasError as atlas_err:
            raise KieError(
                f"kie.ai AND AtlasCloud both failed. "
                f"kie={kie_err!s} | atlas={atlas_err!s}"
            ) from atlas_err
