"""Row-level data classes shared by both row processors.

The Sheet payloads land here after parsing; the orchestrator returns a
``RowResult`` with everything the sheet-writer needs to fill the row plus
metadata for the SYMPHONY_DB log.

Plan §15 Appendix A (sheet column maps) and Phase 7 (metadata).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bulkvid.pipeline.market import effective_country
from bulkvid.pipeline.urls import normalize_url

# Status codes — one per failure mode the orchestrator can return.
STATUS_SUCCESS = "SUCCESS"
STATUS_ARTICLE_FETCH_FAILED = "ARTICLE_FETCH_FAILED"
STATUS_IMAGE_DOWNLOAD_FAILED = "IMAGE_DOWNLOAD_FAILED"
STATUS_IMAGE_GEN_FAILED = "IMAGE_GEN_FAILED"
STATUS_TTS_FAILED = "TTS_FAILED"
STATUS_VIDEO_ASSEMBLY_FAILED = "VIDEO_ASSEMBLY_FAILED"
STATUS_ZAPCAP_FAILED_KEPT_NO_CAPTIONS = "ZAPCAP_FAILED_KEPT_NO_CAPTIONS"
STATUS_STORAGE_FAILED = "STORAGE_FAILED"
STATUS_INTERNAL_ERROR = "INTERNAL_ERROR"
STATUS_ROW_TIMEOUT = "ROW_TIMEOUT"
# Operator hit "Kill job" or "Stop all jobs" while this row was pending or
# in-flight. Distinguished from STATUS_INTERNAL_ERROR so the sidebar can
# render "killed by user" instead of a generic failure.
# Plan: ``_plans/2026-06-14-stuck-processing-rows.md`` §B.
STATUS_KILLED_BY_USER = "KILLED_BY_USER"


# ── Target-market normalization ──────────────────────────────────────────────


class _MarketRow:
    """Normalizes a row's article URL, and fills a blank ``country`` from it.

    The URL is scheme-corrected first (``www.x.com/p`` -> ``https://www.x.com/p``).
    A pasted cell routinely has no scheme because browsers add it silently, and
    ``ArticleFetcher.fetch`` rejected those outright — chat 2026-08-17, a whole
    batch failed with ``Invalid URL: 'www.drexur.com/dsr?q=...'`` and produced
    no video at all. Doing it here means the stored payload, the logs and every
    consumer see the same corrected URL, not just the fetcher.

    A row pasted as ``...?q=seized%20cars%20ireland&locale=en_IE`` with the
    Country column left empty is still an Irish row, but nothing downstream
    could see that: ``locale=`` was read for its language half only, so the
    region never reached ``accent_directive`` (which does know ``ie -> Irish``)
    or the script's market context. The row shipped in a default voice written
    for nowhere in particular.

    Normalizing here rather than in the route builders is deliberate: every
    construction path goes through the dataclass ``__init__`` — the
    ``_build_*_row`` builders, ``_row_from_payload`` on worker replay, and the
    local worker — so a tab added later cannot forget it. An explicit Country
    column always wins; this only ever fills a blank, and is never written back
    to the sheet.

    Field-less on purpose: a mixin that declared fields would shift positional
    argument order in every existing ``Row(...)`` call site.

    Plan: ``_plans/2026-08-17-locale-market-accent.md``.
    """

    article_url: str
    country: str

    def __post_init__(self) -> None:
        self.article_url = normalize_url(self.article_url)
        self.country = effective_country(self.article_url, self.country)


@dataclass
class ImageVORow(_MarketRow):
    """Image-VO tab input row (plan §15 Appendix A)."""

    row_num: int
    country: str
    vertical: str
    article_url: str
    manual_image_url: str
    voice_over: bool                  # default True
    zapcap: bool                      # default False on this tab
    aspect_ratio: str                 # e.g. "9:16"
    script_pattern: str               # e.g. "How To"
    open_comments: str                # highest-priority signal


@dataclass
class OneClickImageVidRow(_MarketRow):
    """``1-click-image-vid`` tab input row.

    One captioned still-image video out. The processor generates a 4-panel story
    collage (nano-banana-2), splits it into 4 quadrants, sequences them into ONE
    video sized to the voiceover, and burns in ZapCap captions. Exactly one
    ``Ready Video`` is written back (col L).

    ``manual_image_url`` is OPTIONAL: filled → the 4 frames are generated FROM
    that seed image (image-to-image); blank → they are generated from scratch
    (text-to-image) grounded in the article + vertical + country.

    Same input columns as :class:`ImageVORow` plus the cartoon-style CTA pair
    (``cta_enabled`` / ``cta_text``) — an optional yellow pill on the final
    video. Plan ``_plans/2026-09-10-one-click-image-vid-tab.md``.
    """

    row_num: int
    country: str
    vertical: str
    article_url: str
    manual_image_url: str
    voice_over: bool                  # default True
    zapcap: bool                      # default False
    aspect_ratio: str                 # e.g. "9:16"
    script_pattern: str
    open_comments: str
    cta_enabled: bool = False         # mirrors simple-motion — yellow pill at bottom
    cta_text: str = ""                # operator text; empty = per-language default


@dataclass
class CardChoice:
    """Per-video card-template selection on the ``simple x4`` tab.

    Empty ``template_id`` means "no card overlay — use the kie-generated
    image as-is" (today's behavior). ``"1"`` / ``"2"`` pick a Pillow-rendered
    overlay; ``cta`` is the button text (empty → fall back to the
    per-template default in the settings registry).
    """

    template_id: str = ""             # "" | "1" | "2"
    cta: str = ""                     # operator text, ≤80 chars


@dataclass
class SimpleX4Row(_MarketRow):
    """Simple x4 tab input row — 4 videos generated from one Manual Image
    via the image_vo pipeline, each with its own optional card overlay.

    Same input columns as Image-VO, plus 4 ``(template, cta)`` pairs (one
    per generated video). ``cards`` is always exactly length 4; entries
    with empty ``template_id`` are rendered without an overlay (matches
    today's behavior). Plan ``_plans/2026-06-08-simple-x4-template-cards.md``.
    """

    row_num: int
    country: str
    vertical: str
    article_url: str
    manual_image_url: str
    voice_over: bool                  # default True
    zapcap: bool                      # default False
    aspect_ratio: str                 # e.g. "9:16"
    script_pattern: str
    cards: list[CardChoice]           # exactly 4
    open_comments: str


@dataclass
class SimpleRow(_MarketRow):
    """Simple tab input row — one video from the user's existing Manual Image.

    Same input columns as Image-VO, but NO image generation: the supplied
    ``manual_image_url`` is resized to the target aspect and turned into a
    single voiceover video. Exactly one ``Ready Video`` is written back.
    """

    row_num: int
    country: str
    vertical: str
    article_url: str
    manual_image_url: str
    voice_over: bool                  # default True
    zapcap: bool                      # default False
    aspect_ratio: str                 # e.g. "9:16"
    script_pattern: str
    open_comments: str


@dataclass
class AvatarRow(_MarketRow):
    """``video with avatar`` tab input row — static background image
    (Manual Image used as-is, or a single kie text-to-image) with a
    TikTok Symphony avatar composited at the bottom-left for the full
    avatar audio duration. Exactly one ``Ready Video`` is written back.

    Pipeline plan: ``_plans/2026-06-09-avatar-static-image-pipeline.md``
    (replaced the original 2-shot Seedance plan).

    ``avatar_size`` / ``avatar_shape`` are operator-facing knobs added
    2026-06-09 (plan
    ``_plans/2026-06-09-avatar-overlay-size-shape.md``). Both default
    to ``""`` so existing sheets that don't have the new columns keep
    rendering today's behaviour (Medium / Rectangle).
    """

    row_num: int
    country: str
    vertical: str
    article_url: str
    manual_image_url: str             # blank → text-to-image; else used as-is
    avatar_id: str                    # TikTok Symphony avatar id (per row)
    voice_over: bool                  # default True — the avatar narrates
    zapcap: bool                      # default False
    aspect_ratio: str                 # e.g. "9:16"
    script_pattern: str
    cta_enabled: bool                 # Yes/No — yellow pill at bottom if Yes
    cta_text: str                     # blank → per-language fallback
    open_comments: str
    # New 2026-06-09. Empty string = use today's default behaviour.
    avatar_size: str = ""             # "" | "small" | "medium" | "large"
    avatar_shape: str = ""            # "" | "rectangle" | "circle"


@dataclass
class TextOnImgRow(_MarketRow):
    """``paste text on img`` tab input row — one IMAGE (not video) from the
    user's Manual Image with the operator-typed ``text`` overlaid in the
    center (heavy white, thick black outline). The composed PNG is written
    back to the ``Ready Image`` column.

    The video pipeline (article fetch → script → TTS → Rendi → ZapCap)
    was stripped on 2026-06-09 per the user's "should produce an image,
    not a video" call. ``article_url`` / ``voice_over`` / ``zapcap`` /
    ``script_pattern`` / ``open_comments`` are retained for wire
    compatibility with the existing Apps Script payload but are ignored
    by the processor.
    """

    row_num: int
    country: str
    vertical: str
    article_url: str                  # ignored — kept for wire compat
    manual_image_url: str
    text: str                         # the overlay text — central to this tab
    voice_over: bool                  # ignored — kept for wire compat
    zapcap: bool                      # ignored — kept for wire compat
    aspect_ratio: str                 # e.g. "9:16"
    script_pattern: str               # ignored — kept for wire compat
    open_comments: str                # ignored — kept for wire compat


@dataclass
class ImageResizeRow(_MarketRow):
    """``image_resize`` tab input row — one IMAGE (not video): the operator's
    Manual Image reframed to the target aspect ratio by Nano Banana 2, which
    extends the background/design naturally and re-renders any baked-in text so
    the result looks native at the new size. The reframed image URL is written
    back to the ``Ready Image`` column.

    Column-identical to :class:`TextOnImgRow` (Yoav cloned that tab's layout).
    ``text`` here is the inherited "Text" column (E) and is **ignored** — the
    text this tab preserves already lives in the image pixels, we do not overlay
    operator-typed text. ``article_url`` / ``voice_over`` / ``zapcap`` /
    ``script_pattern`` / ``open_comments`` are likewise retained for Apps Script
    payload compatibility but ignored by the processor.

    Plan ``_plans/2026-07-20-image-resize-tab.md``.
    """

    row_num: int
    country: str
    vertical: str
    article_url: str                  # ignored — kept for wire compat
    manual_image_url: str
    text: str                         # ignored — text is baked into the image
    voice_over: bool                  # ignored — kept for wire compat
    zapcap: bool                      # ignored — kept for wire compat
    aspect_ratio: str                 # target size, e.g. "9:16" or "1080x1920"
    script_pattern: str               # ignored — kept for wire compat
    open_comments: str                # ignored — kept for wire compat


@dataclass
class CartoonRow(_MarketRow):
    """Cartoon tab input row — animated, multi-shot videos generated from text.

    Same input columns as Image-VO (the "Manual Image" column is present in the
    sheet but ignored: cartoon scenes are generated from scratch, no seed),
    PLUS two CTA columns (Yoav 2026-06-08):
      * ``cta_enabled`` — operator picks Yes/No on the Sheet's CTA column.
        When True, a yellow CTA pill is overlaid at the bottom of every
        generated cartoon video.
      * ``cta_text`` — operator's CTA text. Empty falls back to the per-
        language "Read More" table (``cta_defaults.default_cta_for_language``).

    Each row produces TWO independent ~6-7s videos, each a stitched sequence of
    short Seedance image-to-video clips. Two ``Ready Video`` cells are written
    back. See ``orchestrator/row_processor_cartoon.py``,
    ``pipeline/cartoon_prompt.py``, and ``pipeline/cartoon_cta.py``.
    """

    row_num: int
    country: str
    vertical: str
    article_url: str
    voice_over: bool                  # default True
    zapcap: bool                      # default False
    aspect_ratio: str                 # e.g. "9:16"
    script_pattern: str
    open_comments: str
    cta_enabled: bool = False         # NEW — default False (no CTA pill)
    cta_text: str = ""                # NEW — operator text; empty = per-language default


@dataclass
class YtCartoonRow(_MarketRow):
    """yt-cartoon tab input row — engaging, variable-length cartoon videos.

    A variable-geometry sibling of :class:`CartoonRow` (the flat-8s ``cartoon``
    tab). Same article-driven, no-seed-image, multi-shot pipeline, PLUS four
    operator knobs the new tab adds after ZapCap (sheet columns G-J):

      * ``tone``         — ``Tone`` cell. Blank → engaging (this tab exists for
        the new lively/clickable narration); an explicit calm-ish word opts
        back into today's calm cartoon delivery. Resolved by
        ``pipeline.yt_cartoon.normalize_tone``.
      * ``cap_position`` — ``Cap Position`` relative-nudge cell shifting the
        ZapCap caption height vs default.
      * ``cta_position`` — ``CTA Position`` relative-nudge cell shifting the
        CTA pill height vs default.
      * ``vid_length``   — ``Vid Length`` cap cell (up to 10 / 15 / 20s). The
        processor scales shots + voiceover to fill it and produces TWO videos
        on the 10s bucket, ONE on 15s/20s. Resolved by
        ``pipeline.yt_cartoon.plan_shots_for_length``.

    Manual Image (D) is present in the sheet but ignored, exactly like cartoon.
    The existing cartoon path is untouched — this is a separate row + processor
    that REUSES the shared planner / TTS-sizing / CTA / ZapCap / Rendi helpers.
    Plan: ``_plans/2026-06-17-yt-cartoon-tab.md``.
    """

    row_num: int
    country: str
    vertical: str
    article_url: str
    voice_over: bool                  # default True
    zapcap: bool                      # default False
    aspect_ratio: str                 # e.g. "9:16"
    script_pattern: str
    open_comments: str
    cta_enabled: bool = False         # CTA pill on/off (mirrors cartoon)
    cta_text: str = ""                # operator text; empty = per-language default
    # NEW yt-cartoon knobs — all blank = today's defaults.
    tone: str = ""                    # "" | "engaging" | "calm"
    cap_position: str = ""            # nudge label (e.g. "Higher")
    cta_position: str = ""            # nudge label
    vid_length: str = ""              # "" | "10" | "15" | "20" (or "up to 15s")


@dataclass
class SimpleMotionRow(_MarketRow):
    """simple-motion tab input row — animate super-realistic images.

    A sibling of :class:`CartoonRow` (same article-driven planner / TTS-sizing /
    CTA / ZapCap / Rendi pipeline) with two differences:

      * Images are SUPER-REALISTIC photographs, not cartoons (the row processor
        prepends ``REALISTIC_STYLE`` instead of ``CARTOON_STYLE`` and uses the
        ``simple_motion_planner_prompt`` which describes photographic scenes).
      * The operator can paste their OWN images. ``manual_image_1`` (sheet col D)
        is shot 1; ``manual_image_2`` (col E) is shot 2. A blank cell is
        auto-generated; a filled cell is animated as-is. So a row produces ONE
        8-second video (two 4s shots stitched), written to Ready Video 1.

    Same CTA columns as cartoon (Yoav 2026-06-08 pattern). The cartoon /
    yt-cartoon orchestration is untouched — this is a separate row + processor
    that REUSES the shared helpers. Plan:
    ``_plans/2026-06-22-simple-motion-tab.md``.
    """

    row_num: int
    country: str
    vertical: str
    article_url: str
    manual_image_1: str               # col D — blank → generate; filled → as-is
    manual_image_2: str               # col E — blank → generate; filled → as-is
    voice_over: bool                  # default True
    zapcap: bool                      # default False
    aspect_ratio: str                 # e.g. "9:16"
    script_pattern: str
    open_comments: str
    cta_enabled: bool = False         # mirrors cartoon — yellow pill at bottom
    cta_text: str = ""                # operator text; empty = per-language default


@dataclass
class GoogleSimpleMotionRow(_MarketRow):
    """google-simple-motion tab input row — N size-variant fixed-script motion ads.

    A sibling of :class:`SimpleMotionRow`. Instead of an article-driven voiceover,
    every video speaks the SAME fixed, localized two-sentence script
    (``pipeline.google_simple_motion``) with a 3s silence between the sentences,
    floored at 11s. A row produces ``num_videos`` (1-4) videos; slot ``i`` is
    rendered at ``aspect_ratios[i]`` (Change Size ``i+1``) and written to Ready
    Video ``i+1`` (cols Q-T). The source image per slot:

      * slot 1 → Manual Image 1 (col D) as-is; blank → generated realistic scene
      * slot 2 → Manual Image 2 (col E) as-is; blank → generated realistic scene
      * slot 3 → AI image, image-to-image using Manual Image 1 as reference
      * slot 4 → AI image, image-to-image using Manual Image 2 as reference

    ``aspect_ratios`` is always length 4 (Change Size 1-4); slots beyond
    ``num_videos`` are ignored, and a blank entry within range defaults to 9:16.
    The subject, opening (random of Explore/Learn/Read more), script, and
    voiceover audio are generated ONCE and shared across all N videos. Plan
    ``_plans/2026-07-20-google-simple-motion-tab.md``.
    """

    row_num: int
    country: str
    vertical: str
    article_url: str
    manual_image_1: str               # col D — slot 1 base; blank → generate
    manual_image_2: str               # col E — slot 2 base; blank → generate
    num_videos: int                   # col F — how many videos (1-4)
    voice_over: bool                  # col G — No → silent motion videos
    zapcap: bool                      # col H
    aspect_ratios: list[str]          # cols I-L (Change Size 1-4); slot i uses [i]
    script_pattern: str               # col M
    open_comments: str                # col P — context for scene generation
    cta_enabled: bool = False         # col N — Yes/No pill
    cta_text: str = ""                # col O — operator text; empty → per-language default


@dataclass
class FastFuriousRow(_MarketRow):
    """fast-and-furious tab input row — N TikTok/Gen-Z variations per row.

    Same columns and N-video / per-slot-aspect layout as
    :class:`GoogleSimpleMotionRow`, but each of the ``num_videos`` (1-4) videos is
    its OWN Gen-Z creative variation (its own lively, fast, modern script), not one
    shared creative rendered at N sizes. Video ``i`` is rendered at
    ``aspect_ratios[i]`` (Change Size ``i+1``) and written to Ready Video ``i+1``
    (cols Q-T).

    ``manual_image_1`` / ``manual_image_2`` (cols D/E) are SHARED by every
    variation as shot 1 / shot 2 (a blank cell generates a realistic scene per
    variation). ``use this script: <text>`` in Open Comments makes every variation
    speak that exact operator text verbatim. Plan
    ``_plans/2026-07-30-fast-and-furious-tab.md``.
    """

    row_num: int
    country: str
    vertical: str
    article_url: str
    manual_image_1: str               # col D — shot 1, shared; blank → generate
    manual_image_2: str               # col E — shot 2, shared; blank → generate
    num_videos: int                   # col F — how many variations (1-4)
    voice_over: bool                  # col G
    zapcap: bool                      # col H
    aspect_ratios: list[str]          # cols I-L (Change Size 1-4); video i uses [i]
    script_pattern: str               # col M
    open_comments: str                # col P — override + scene context
    cta_enabled: bool = False         # col N — Yes/No pill
    cta_text: str = ""                # col O — operator text; empty → per-language default


@dataclass
class MotionAdsRow(_MarketRow):
    """Motion_Ads tab input row — a silent motion-ad video + ad copy.

    Produces ONE 12-second SILENT video (no voiceover, no CTA, no captions, no
    audio track) from a single realistic image, plus two text outputs generated
    from the article: a Headline (sheet col D, <=60 chars) and a Description
    (col E, <=80 chars) written back by the sheet writer.

      * ``manual_image_url`` (col F) — blank -> generate a realistic image sized
        to ``aspect_ratio``; filled -> animate that image as-is.
      * ``apple`` (col G) — Yes -> a GENERATED image must contain NO people
        (Apple/Taboola motion-ad convention). Ignored for a pasted manual image.

    Only the article is required. The cartoon / simple-motion pipelines are
    untouched — this is a separate row + processor that reuses the shared kie
    image + Seedance helpers. Plan ``_plans/2026-07-08-motion-ads-tab.md``.
    """

    row_num: int
    country: str
    vertical: str
    article_url: str
    manual_image_url: str             # col F — blank → generate; filled → as-is
    apple: bool                       # col G — Yes → generated image has no people
    aspect_ratio: str                 # col H "Change Size" — default 16:9
    open_comments: str                # col I — context/directives


@dataclass
class HookCardRow(_MarketRow):
    """Hook_Card tab input row — a 9:16 slideshow with a fixed lower-third hook
    box + background music (no voiceover).

    Produces ONE short vertical video: 1-5 background scenes, each Ken Burns
    zoomed, cut in sequence under a fixed black semi-transparent rounded box
    holding one bold white hook line, with a bundled royalty-free track.

      * ``num_images`` (col D) — AI image count when ALL media cells are blank.
      * ``text`` (col E) — the hook; blank -> generate one in the market language.
      * ``voice_over`` (col F) — Yes -> narrate a script from the article (like
        the other tabs); the music is ducked under the narration and the
        narration drives the video length.
      * ``music`` (col G) — a bundled track ("Uplifting 2"); "None" -> silent;
        blank -> random.
      * ``manual_media`` (cols H-L) — per cell: an image URL (Ken Burns), a video
        URL (used as a clip), ``"AI"`` (AI image) or ``"AI Video"`` (Seedance
        clip). All blank -> ``num_images`` AI images.

    Only the article is required. Reuses the shared kie image + Rendi + script/
    TTS helpers; every other pipeline is untouched. Plan
    ``_plans/2026-07-13-hook-card-tab.md``.
    """

    row_num: int
    country: str
    vertical: str
    article_url: str
    num_images: int                   # col D — AI image count when media blank
    text: str                         # col E — hook; blank → generate
    voice_over: bool                  # col F — Yes → narrate the article
    music: str                        # col G — track; "None" silent; blank random
    manual_media: list[str]           # cols H-L — URL (image/video) or AI keyword
    aspect_ratio: str                 # col M "Change Size" — default 9:16
    open_comments: str                # col N — context/directives


@dataclass
class FourImagesVO2Row(_MarketRow):
    """4Images-VO2 tab input row (plan §15 Appendix A)."""

    row_num: int
    country: str
    vertical: str
    article_url: str
    how_many: int                     # 1..4
    voice_over: bool                  # default True
    image_urls: list[str]             # exactly how_many URLs
    zapcap: bool
    aspect_ratio: str
    script_pattern: str
    open_comments: str


@dataclass
class RowResult:
    """What the row processor hands back to the sheet writer + metadata log."""

    row_num: int
    status: str
    video_urls: list[str] = field(default_factory=list)   # Ready Video 1..4
    cost_usd: float = 0.0
    elapsed_seconds: float = 0.0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Text outputs written back to their own named columns (Motion_Ads tab:
    # Headline -> col D, Description -> col E). Empty on every other tab, whose
    # rows never populate them and whose sheets have no such columns. The sheet
    # writer resolves the target column by header name and only writes when the
    # value is non-empty. Plan ``_plans/2026-07-08-motion-ads-tab.md``.
    headline: str = ""
    description: str = ""
