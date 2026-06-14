"""Resolve a row's aspect_ratio field at processor entry.

The "Change Size" column on every manual-image tab can be left blank by the
operator, in which case "use the manual image as-is" is the intended
behaviour. This helper centralises that decision:

  - non-empty input → return verbatim (operator picked a size, respect it)
  - blank + manual image URL → probe the image, return ``"WxH"`` in pixels
  - blank + no URL → return ``fallback`` (default ``"9:16"``)

The returned string flows back into ``row.aspect_ratio``. Downstream:
  * ``rendi.dimensions_for_ratio`` already accepts ``"WxH"`` and passes
    through the exact pixel size for video assembly.
  * ``rendi.normalize_aspect_ratio`` reduces ``"WxH"`` to the closest
    valid model ratio for kie / Seedance scene generation.

Never raises. A probe failure is logged at WARNING and falls back to
``fallback`` so the row keeps moving — the operator's "blank = native"
preference is best-effort, not a hard requirement.

Plan: ``_plans/2026-06-14-blank-size-uses-native-image.md`` §D.4.
"""

from __future__ import annotations

from bulkvid.image_probe import probe_native_dimensions
from bulkvid.logging import get_logger

_log = get_logger("aspect")


async def resolve_aspect_ratio(
    raw: str,
    *,
    manual_image_url: str | None,
    row_num: int,
    fallback: str = "9:16",
) -> str:
    """Resolve a row's aspect_ratio per the "blank → native image" rule.

    Three branches:
      1. ``raw`` non-empty → return ``raw`` unchanged (operator override).
      2. ``raw`` blank, ``manual_image_url`` given → probe, return ``"WxH"``.
         On probe failure, fall back to ``fallback``.
      3. ``raw`` blank, no ``manual_image_url`` → return ``fallback``.
    """
    s = (raw or "").strip()
    if s:
        _log.info(
            "skipped",
            row=row_num,
            reason="user_set",
            value=s[:32],
        )
        return s

    if not manual_image_url:
        _log.info(
            "skipped",
            row=row_num,
            reason="no_manual_image",
            fallback=fallback,
        )
        return fallback

    dims = await probe_native_dimensions(manual_image_url)
    if dims is None:
        _log.warning(
            "probe_failed_using_fallback",
            row=row_num,
            fallback=fallback,
        )
        return fallback

    w, h = dims
    resolved = f"{w}x{h}"
    _log.info(
        "from_native",
        row=row_num,
        w=w,
        h=h,
        resolved=resolved,
    )
    return resolved
