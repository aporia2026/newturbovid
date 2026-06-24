"""Pure helpers for the ``yt-cartoon`` tab.

The yt-cartoon tab is a variable-geometry sibling of the flat-8s ``cartoon``
tab. Everything here is pure (no I/O, no API calls) so the load-bearing
duration math, tone selection, and position nudges are trivially unit-tested
in isolation — the council flagged the duration math as the #1 technical risk,
so it lives here behind a single function, decided on paper first.

Three knobs the new tab adds, all resolved here:

  * ``Vid Length``  → :func:`plan_shots_for_length` packs a target duration into
    Seedance-legal clips (the model only generates 4/8/12s; the Rendi concat
    TRIMS each clip down to an arbitrary ``per_clip_seconds`` and forces the
    total via ``-t``, so any exact target is reachable). Also derives how many
    videos the row produces and the per-bucket voiceover word budget.
  * ``Tone``        → :func:`normalize_tone` maps the cell to a named entry in
    a small prompt registry (calm = today's cartoon prompt; engaging = the new
    lively/clickable prompt). Blank defaults to ``engaging`` (Yoav 2026-06-17:
    this tab exists for the new style).
  * ``Cap/CTA Position`` → :func:`cap_position_top_offset` /
    :func:`cta_position_margin_delta` translate a relative-nudge dropdown
    label into an offset applied to the ZapCap ``top`` percent and the CTA
    pill's bottom-margin fraction respectively.

Plan: ``_plans/2026-06-17-yt-cartoon-tab.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Vid Length ───────────────────────────────────────────────────────────────

# Selectable caps (seconds). Blank / unrecognised → the smallest bucket so a
# lazy/blank cell still ships a sane short video rather than the longest,
# most expensive one.
VID_LENGTH_DEFAULT = 10
VID_LENGTH_BUCKETS: tuple[int, ...] = (10, 15, 20)

# Spoken words per second used to size the per-bucket word budget. The cartoon
# tab uses ~1.5 wps (its SLOW-end estimate) because an 8s clip can absorb a
# little trailing silence. yt-cartoon's longer buckets cannot: at 1.5 wps a
# fast/engaging delivery finishes the line in ~8s and leaves ~10s of dead air on
# a 20s video (the bug Yoav hit on job-316c46f420f2371b). 2.3 wps roughly
# doubles the narration (≈45 words on the 20s bucket vs 29) so the line actually
# fills the window; the processor's atempo + shorten-and-retry path is the
# backstop when a slow delivery overruns, and ``fit_video_to_vo`` trims any
# residual gap so there is never significant dead air.
WORDS_PER_SECOND = 2.3

# Trailing silence dwell after the voiceover, mirrors the cartoon constant
# (``VO_TAIL_SECONDS``). The effective VO window is ``target - tail``.
VO_TAIL_SECONDS = 0.5

# Floor on the rendered video length once it is shrunk to the narration (see
# ``fit_video_to_vo``). Stops a pathologically short VO from producing a
# blink-length clip whose shots can't breathe.
MIN_VIDEO_SECONDS = 6.0

# Seedance only generates these clip lengths (seconds). We always generate the
# SMALLEST legal tier that still covers a shot's trimmed length, so a 3.3s or
# 4.0s shot is generated at the cheap 4s tier — minimal paid-API spend.
SEEDANCE_LEGAL_DURATIONS: tuple[int, ...] = (4, 8, 12)

# Per-bucket shot count + videos-per-row. Yoav-confirmed table
# (2026-06-17): more shots on longer videos = more visual variety/retention,
# and a single video on the 15s/20s buckets keeps cost/time down vs the 10s
# bucket's two videos. Kept as an explicit table (not a formula) so the
# shipped behaviour is exactly what was signed off.
_SHOTS_BY_BUCKET: dict[int, int] = {10: 3, 15: 4, 20: 5}
_VIDEOS_BY_BUCKET: dict[int, int] = {10: 2, 15: 1, 20: 1}

# Floor on per-bucket min words so even the shortest bucket asks for a real
# sentence (mirrors the cartoon planner's CARTOON_MIN_WORDS rationale).
_MIN_WORDS_FLOOR = 6


@dataclass(frozen=True)
class ShotPlan:
    """The full render geometry for one Vid Length bucket.

    ``per_clip_seconds`` is the trim length the Rendi concat applies to each
    clip (they sum to ``target_seconds``). ``seedance_durations`` is the legal
    4/8/12 tier each clip is GENERATED at before trimming. The cartoon tab is
    the special case ``num_shots=2, per_clip_seconds=[4.0, 4.0]`` — but cartoon
    does not route through here; this is yt-cartoon only.
    """

    bucket_seconds: int
    target_seconds: float
    num_shots: int
    per_clip_seconds: list[float]
    seedance_durations: list[int]
    num_videos: int
    target_words: int
    min_words: int
    max_words: int
    max_effective_vo: float


def normalize_vid_length(raw: object) -> int:
    """Coerce a ``Vid Length`` cell to one of ``VID_LENGTH_BUCKETS``.

    Accepts ``"up to 15 seconds"``, ``"15s"``, ``"15"``, ``15`` etc. — anything
    whose first run of digits matches a known bucket. Blank / garbage falls
    back to ``VID_LENGTH_DEFAULT`` so a lazy or malformed cell never 400s the
    batch (matches the avatar tab's defensive enum coercion).
    """
    if raw is None:
        return VID_LENGTH_DEFAULT
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        n = int(raw)
        return n if n in VID_LENGTH_BUCKETS else VID_LENGTH_DEFAULT
    s = str(raw).strip().lower()
    if not s:
        return VID_LENGTH_DEFAULT
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return VID_LENGTH_DEFAULT
    try:
        n = int(digits)
    except ValueError:
        return VID_LENGTH_DEFAULT
    return n if n in VID_LENGTH_BUCKETS else VID_LENGTH_DEFAULT


def videos_for_vid_length(bucket: int) -> int:
    """How many independent videos a row produces for a bucket (2 on 10s,
    1 on 15s/20s). Unknown buckets default to 1 (the conservative, cheap case)."""
    return _VIDEOS_BY_BUCKET.get(bucket, 1)


def _smallest_legal_duration(seconds: float) -> int:
    """Smallest Seedance tier (4/8/12) that fully covers ``seconds``."""
    for d in SEEDANCE_LEGAL_DURATIONS:
        if seconds <= d:
            return d
    return SEEDANCE_LEGAL_DURATIONS[-1]


def plan_shots_for_length(raw: object) -> ShotPlan:
    """Full render geometry for a ``Vid Length`` cell. Never raises.

    Distributes the bucket's target seconds evenly across its shot count, picks
    the cheapest Seedance gen tier per shot, and sizes the voiceover word
    budget to fill the effective window (target minus the trailing dwell) at
    the slow-delivery words-per-second rate.
    """
    bucket = normalize_vid_length(raw)
    num_shots = _SHOTS_BY_BUCKET.get(bucket, 3)
    target = float(bucket)
    per_clip = round(target / num_shots, 3)
    per_clip_seconds = [per_clip] * num_shots
    # Correct any rounding drift onto the last shot so the trims sum exactly to
    # the target (the concat forces the total via -t, but keeping the parts
    # honest avoids a sliver of held-last-frame).
    drift = round(target - sum(per_clip_seconds), 3)
    per_clip_seconds[-1] = round(per_clip_seconds[-1] + drift, 3)
    seedance_durations = [_smallest_legal_duration(d) for d in per_clip_seconds]

    max_effective_vo = round(target - VO_TAIL_SECONDS, 3)
    target_words = max(_MIN_WORDS_FLOOR, round(max_effective_vo * WORDS_PER_SECOND))
    min_words = max(_MIN_WORDS_FLOOR, target_words - 4)
    max_words = target_words + 4

    return ShotPlan(
        bucket_seconds=bucket,
        target_seconds=target,
        num_shots=num_shots,
        per_clip_seconds=per_clip_seconds,
        seedance_durations=seedance_durations,
        num_videos=videos_for_vid_length(bucket),
        target_words=target_words,
        min_words=min_words,
        max_words=max_words,
        max_effective_vo=max_effective_vo,
    )


def fit_video_to_vo(
    effective_vo: float, plan: ShotPlan, *, has_vo: bool
) -> tuple[float, list[float]]:
    """Shrink the rendered video to the actual narration length.

    Returns ``(total_video_seconds, per_clip_seconds)``. The cartoon tab forces
    a flat length and tolerates trailing silence; yt-cartoon's longer buckets
    must NOT, so we set the video length to the measured voiceover plus the
    dwell tail, capped at the bucket target and floored at ``MIN_VIDEO_SECONDS``.
    The per-clip trims are redistributed evenly across the (fixed) shot count so
    they sum to the new length and every shot still appears (the Seedance clips
    were generated at the 4s tier, which always covers a per-clip <= 4s).

    ``has_vo=False`` (no voiceover) keeps the full bucket — a silent video has
    no narration to track, so it stays at its planned length.
    """
    if not has_vo:
        return plan.target_seconds, list(plan.per_clip_seconds)
    total = min(
        plan.target_seconds,
        max(MIN_VIDEO_SECONDS, round(effective_vo + VO_TAIL_SECONDS, 3)),
    )
    per = round(total / plan.num_shots, 3)
    per_clip = [per] * plan.num_shots
    drift = round(total - sum(per_clip), 3)
    per_clip[-1] = round(per_clip[-1] + drift, 3)
    return total, per_clip


# ── Tone ─────────────────────────────────────────────────────────────────────

TONE_ENGAGING = "engaging"
TONE_CALM = "calm"

# Cells that mean "use today's calm cartoon narration". Anything else — INCLUDING
# blank — resolves to engaging, because this tab exists for the new style
# (Yoav 2026-06-17). Kept as a registry, not a hard binary, so adding angles
# later (curiosity / problem-agitate / testimonial) is a small config edit.
_CALM_ALIASES = frozenset(
    {"calm", "current", "standard", "plain", "podcast", "normal", "default"}
)


def normalize_tone(raw: object) -> str:
    """Map a ``Tone`` cell to ``TONE_CALM`` or ``TONE_ENGAGING``.

    Blank or unrecognised → ``TONE_ENGAGING`` (this tab's purpose). Only an
    explicit calm-ish word opts back into today's calm narration.
    """
    v = str(raw or "").strip().lower()
    if v in _CALM_ALIASES:
        return TONE_CALM
    return TONE_ENGAGING


# ── Caption / CTA position nudges ────────────────────────────────────────────

# Caption nudge → percentage-point offset applied to the ZapCap ``top`` value.
# Lower ``top`` = higher on screen, so "Higher" is a NEGATIVE offset. Applied
# on top of the base (70 default, or 30 when a CTA pill is present) and clamped
# at the apply site.
_CAP_POSITION_OFFSETS: dict[str, int] = {
    "much higher": -16,
    "higher": -8,
    "": 0,
    "default": 0,
    "lower": 8,
    "much lower": 16,
}
CAP_TOP_MIN = 5
CAP_TOP_MAX = 90

# CTA nudge → delta added to the pill's bottom-margin fraction. A LARGER bottom
# margin lifts the pill UP, so "Higher" is a POSITIVE delta. Clamped at the
# apply site.
_CTA_POSITION_MARGIN_DELTAS: dict[str, float] = {
    "much higher": 0.06,
    "higher": 0.03,
    "": 0.0,
    "default": 0.0,
    "lower": -0.03,
    "much lower": -0.06,
}
CTA_MARGIN_MIN = 0.05
CTA_MARGIN_MAX = 0.40


def cap_position_top_offset(raw: object) -> int:
    """Percentage-point offset for the ZapCap ``top`` from a nudge label.
    Unknown / blank → 0 (today's default position)."""
    return _CAP_POSITION_OFFSETS.get(str(raw or "").strip().lower(), 0)


def cta_position_margin_delta(raw: object) -> float:
    """Bottom-margin-fraction delta for the CTA pill from a nudge label.
    Unknown / blank → 0.0 (today's default position)."""
    return _CTA_POSITION_MARGIN_DELTAS.get(str(raw or "").strip().lower(), 0.0)


def resolve_cap_top(base_top: int, raw: object) -> int:
    """Apply a caption nudge to a base ``top`` percent and clamp to a sane band."""
    return max(CAP_TOP_MIN, min(CAP_TOP_MAX, base_top + cap_position_top_offset(raw)))


def resolve_cta_margin(base_margin: float, raw: object) -> float:
    """Apply a CTA nudge to a base bottom-margin fraction and clamp."""
    return max(
        CTA_MARGIN_MIN, min(CTA_MARGIN_MAX, base_margin + cta_position_margin_delta(raw))
    )
