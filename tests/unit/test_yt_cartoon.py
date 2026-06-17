"""Unit tests for the yt-cartoon tab.

Covers the load-bearing pure helpers (the council's #1 risk: the Vid Length →
clip-duration math), the tone registry, the position nudges, a payload
round-trip for ``YtCartoonRow``, and — critically — a REGRESSION GUARD proving
the parameterised ``compute_atempo`` / planner helpers are byte-identical for
the cartoon tab's defaults.
"""

from __future__ import annotations

import json

from bulkvid.models.row import YtCartoonRow
from bulkvid.orchestrator.queue import (
    TAB_YT_CARTOON,
    _row_to_payload,
    payload_to_row,
)
from bulkvid.orchestrator.row_processor_cartoon import (
    MAX_EFFECTIVE_VO_SECONDS,
    compute_atempo,
)
from bulkvid.pipeline import yt_cartoon as yc

# ── Vid Length normalisation ─────────────────────────────────────────────────


def test_normalize_vid_length_accepts_known_buckets() -> None:
    assert yc.normalize_vid_length("10") == 10
    assert yc.normalize_vid_length("15") == 15
    assert yc.normalize_vid_length("20") == 20
    assert yc.normalize_vid_length(15) == 15


def test_normalize_vid_length_parses_dropdown_labels() -> None:
    assert yc.normalize_vid_length("up to 10 seconds") == 10
    assert yc.normalize_vid_length("up to 15s") == 15
    assert yc.normalize_vid_length("  20s ") == 20


def test_normalize_vid_length_blank_and_garbage_default_to_smallest() -> None:
    for bad in ("", "   ", None, "twelve", "999", "abc", "0"):
        assert yc.normalize_vid_length(bad) == yc.VID_LENGTH_DEFAULT == 10


def test_videos_per_bucket_matches_signed_off_table() -> None:
    assert yc.videos_for_vid_length(10) == 2
    assert yc.videos_for_vid_length(15) == 1
    assert yc.videos_for_vid_length(20) == 1


# ── plan_shots_for_length (the load-bearing math) ───────────────────────────


def test_plan_10s_two_videos_three_shots_fills_target() -> None:
    plan = yc.plan_shots_for_length("10")
    assert plan.bucket_seconds == 10
    assert plan.num_videos == 2
    assert plan.num_shots == 3
    assert len(plan.per_clip_seconds) == 3
    # Trims sum EXACTLY to the target (drift corrected onto the last shot).
    assert round(sum(plan.per_clip_seconds), 3) == 10.0
    # Every shot generated at the cheap 4s Seedance tier.
    assert plan.seedance_durations == [4, 4, 4]
    assert plan.max_effective_vo == 9.5


def test_plan_15s_single_video_four_shots() -> None:
    plan = yc.plan_shots_for_length("up to 15s")
    assert plan.num_videos == 1
    assert plan.num_shots == 4
    assert round(sum(plan.per_clip_seconds), 3) == 15.0
    assert plan.seedance_durations == [4, 4, 4, 4]
    assert plan.max_effective_vo == 14.5


def test_plan_20s_single_video_five_shots_zero_waste() -> None:
    plan = yc.plan_shots_for_length("20")
    assert plan.num_videos == 1
    assert plan.num_shots == 5
    # 20/5 = exactly 4.0s per clip — generated at 4s, no trim waste.
    assert plan.per_clip_seconds == [4.0, 4.0, 4.0, 4.0, 4.0]
    assert plan.seedance_durations == [4, 4, 4, 4, 4]
    assert plan.max_effective_vo == 19.5


def test_plan_word_budget_scales_with_length() -> None:
    w10 = yc.plan_shots_for_length("10").target_words
    w15 = yc.plan_shots_for_length("15").target_words
    w20 = yc.plan_shots_for_length("20").target_words
    assert w10 < w15 < w20
    # Sized at ~1.5 words/sec of effective window, min<=target<=max.
    for raw in ("10", "15", "20"):
        p = yc.plan_shots_for_length(raw)
        assert p.min_words <= p.target_words <= p.max_words
        assert p.min_words >= yc._MIN_WORDS_FLOOR


def test_plan_seedance_durations_are_always_legal() -> None:
    for raw in ("10", "15", "20", "", "garbage"):
        p = yc.plan_shots_for_length(raw)
        for d in p.seedance_durations:
            assert d in yc.SEEDANCE_LEGAL_DURATIONS


def test_smallest_legal_duration_packs_correctly() -> None:
    assert yc._smallest_legal_duration(3.3) == 4
    assert yc._smallest_legal_duration(4.0) == 4
    assert yc._smallest_legal_duration(4.1) == 8
    assert yc._smallest_legal_duration(8.0) == 8
    assert yc._smallest_legal_duration(10.0) == 12
    assert yc._smallest_legal_duration(99.0) == 12   # clamps to max tier


# ── Tone registry ────────────────────────────────────────────────────────────


def test_tone_blank_defaults_to_engaging() -> None:
    for blank in ("", "   ", None):
        assert yc.normalize_tone(blank) == yc.TONE_ENGAGING


def test_tone_calm_aliases() -> None:
    for calm in ("calm", "Calm", "CURRENT", "standard", "podcast", "plain"):
        assert yc.normalize_tone(calm) == yc.TONE_CALM


def test_tone_engaging_aliases_and_unknown() -> None:
    for eng in ("engaging", "Engaging", "lively", "clickable", "anything-else"):
        assert yc.normalize_tone(eng) == yc.TONE_ENGAGING


# ── Position nudges ──────────────────────────────────────────────────────────


def test_cap_position_offsets_and_direction() -> None:
    # Lower top% = higher on screen, so "Higher" is a negative offset.
    assert yc.cap_position_top_offset("Higher") == -8
    assert yc.cap_position_top_offset("Much Higher") == -16
    assert yc.cap_position_top_offset("Lower") == 8
    assert yc.cap_position_top_offset("") == 0
    assert yc.cap_position_top_offset("nonsense") == 0


def test_resolve_cap_top_clamps() -> None:
    assert yc.resolve_cap_top(70, "Higher") == 62
    assert yc.resolve_cap_top(70, "Much Lower") == 86
    # Clamp band.
    assert yc.resolve_cap_top(10, "Much Higher") == yc.CAP_TOP_MIN   # 10-16 -> 5
    assert yc.resolve_cap_top(85, "Much Lower") == yc.CAP_TOP_MAX     # 85+16 -> 90


def test_cta_position_margin_direction_and_clamp() -> None:
    # Larger bottom margin lifts the pill UP, so "Higher" is a positive delta.
    assert round(yc.resolve_cta_margin(0.19, "Higher"), 3) == 0.22
    assert round(yc.resolve_cta_margin(0.19, "Much Lower"), 3) == 0.13
    assert round(yc.resolve_cta_margin(0.19, ""), 3) == 0.19
    # Clamp band.
    assert yc.resolve_cta_margin(0.06, "Much Lower") == yc.CTA_MARGIN_MIN
    assert yc.resolve_cta_margin(0.38, "Much Higher") == yc.CTA_MARGIN_MAX


# ── Payload round-trip ───────────────────────────────────────────────────────


def test_yt_cartoon_row_payload_round_trip() -> None:
    row = YtCartoonRow(
        row_num=7,
        country="MX",
        vertical="automotive",
        article_url="https://example.com/a",
        voice_over=True,
        zapcap=True,
        aspect_ratio="9:16",
        script_pattern="",
        open_comments="punchy please",
        cta_enabled=True,
        cta_text="Read More",
        tone="Engaging",
        cap_position="Higher",
        cta_position="Lower",
        vid_length="up to 20s",
    )
    payload = json.loads(_row_to_payload(row, TAB_YT_CARTOON))
    assert payload["__tab__"] == TAB_YT_CARTOON
    restored = payload_to_row(payload)
    assert isinstance(restored, YtCartoonRow)
    assert restored == row


# ── Regression guard: cartoon defaults are byte-identical ───────────────────


def test_compute_atempo_default_max_effective_is_unchanged() -> None:
    """The new ``max_effective`` kwarg defaults to the cartoon constant, so a
    no-arg call MUST equal an explicit-default call across the full curve —
    this is the firewall proving cartoon's atempo behaviour did not move."""
    for raw in (0.0, 3.0, 7.5, 7.50001, 9.0, 12.0, 16.0):
        assert compute_atempo(raw) == compute_atempo(
            raw, max_effective=MAX_EFFECTIVE_VO_SECONDS
        )


def test_compute_atempo_larger_window_lets_longer_vo_play_natural() -> None:
    """A 9s VO is sped up to fit cartoon's 7.5s window, but plays at natural
    speed inside yt-cartoon's larger 19.5s (20s-bucket) window."""
    atempo_default, eff_default = compute_atempo(9.0)
    assert atempo_default > 1.0                              # sped up to fit
    assert eff_default == MAX_EFFECTIVE_VO_SECONDS           # lands at the cap
    atempo_long, eff_long = compute_atempo(9.0, max_effective=19.5)
    assert atempo_long == 1.0                                # plays natural
    assert eff_long == 9.0
