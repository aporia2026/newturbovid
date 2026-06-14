"""Tests for ``resolve_aspect_ratio`` — the orchestrator entry-point helper
that translates a blank "Change Size" cell into the manual image's native
pixel dimensions.

Plan: ``_plans/2026-06-14-blank-size-uses-native-image.md`` §D.4.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bulkvid.orchestrator.aspect_resolve import resolve_aspect_ratio

URL = "https://example.com/ad.png"


async def test_returns_raw_when_user_set() -> None:
    """Operator picked a value → return it verbatim, no probe."""
    with patch(
        "bulkvid.orchestrator.aspect_resolve.probe_native_dimensions"
    ) as mock_probe:
        out = await resolve_aspect_ratio(
            "9:16", manual_image_url=URL, row_num=2,
        )
    assert out == "9:16"
    assert mock_probe.call_count == 0


async def test_probes_when_blank_and_url_present() -> None:
    """Blank cell + manual image → probe → return ``WxH``."""
    with patch(
        "bulkvid.orchestrator.aspect_resolve.probe_native_dimensions",
        return_value=(1080, 1350),
    ):
        out = await resolve_aspect_ratio(
            "", manual_image_url=URL, row_num=2,
        )
    assert out == "1080x1350"


@pytest.mark.parametrize("blank_url", [None, ""])
async def test_falls_back_when_blank_and_no_url(blank_url) -> None:
    """Blank cell, no URL (e.g. cartoon / avatar text-to-image) → fallback,
    no probe attempted."""
    with patch(
        "bulkvid.orchestrator.aspect_resolve.probe_native_dimensions"
    ) as mock_probe:
        out = await resolve_aspect_ratio(
            "", manual_image_url=blank_url, row_num=2,
        )
    assert out == "9:16"
    assert mock_probe.call_count == 0


async def test_falls_back_when_probe_fails() -> None:
    """Probe returning ``None`` → fall back to default ratio (row keeps moving)."""
    with patch(
        "bulkvid.orchestrator.aspect_resolve.probe_native_dimensions",
        return_value=None,
    ):
        out = await resolve_aspect_ratio(
            "", manual_image_url=URL, row_num=2,
        )
    assert out == "9:16"


async def test_custom_fallback_is_respected() -> None:
    """Caller-supplied fallback is used on the no-URL branch."""
    out = await resolve_aspect_ratio(
        "", manual_image_url=None, row_num=2, fallback="1:1",
    )
    assert out == "1:1"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("9:16", "9:16"),
        ("  9:16  ", "9:16"),    # whitespace trimmed
        ("1080x1920", "1080x1920"),
        ("21:9", "21:9"),
    ],
)
async def test_user_set_values_are_passed_through(raw: str, expected: str) -> None:
    """Various non-blank inputs pass through unchanged (after whitespace trim)."""
    with patch(
        "bulkvid.orchestrator.aspect_resolve.probe_native_dimensions"
    ) as mock_probe:
        out = await resolve_aspect_ratio(
            raw, manual_image_url=URL, row_num=2,
        )
    assert out == expected
    assert mock_probe.call_count == 0
