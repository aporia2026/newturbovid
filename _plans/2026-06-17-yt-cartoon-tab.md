# yt-cartoon tab

Date: 2026-06-17
Status: BUILT (council-reviewed, Yoav-approved, implemented + tested)
Author: Claude + Yoav (council-reviewed)

## Resolved decisions (Yoav 2026-06-17)

1. Tab layout: 4 new columns G Tone / H Cap Position / I CTA Position /
   J Vid Length (confirmed against screenshot).
2. Positions: relative-nudge dropdowns (Much Higher..Much Lower).
3. Vid Length: cap; scale shots+VO; 2 videos on 10s, 1 on 15s/20s.
4. Tone: per-row dropdown (column G); blank → ENGAGING (this tab's purpose).
5. Platform: paid native (Taboola/Outbrain) + YouTube Shorts → guardrails
   designed to the stricter (paid-native) bar so output is safe on both.

## Goal

Add a new `yt-cartoon` Sheet tab: a variant of the existing `cartoon` tab that
adds per-row control over narration tone, caption height, CTA-pill height, and
video length, plus a much more lively/engaging/clickable (but ad-policy-safe)
narration option.

## Why a variant, not a change to `cartoon`

The existing `cartoon` tab is live, paid-API-metered, and its row processor
(`process_cartoon_row`) is full of hard-won edge-case fixes around a hardcoded
flat 8.0s geometry (`TARGET_VIDEO_SECONDS`, `MAX_EFFECTIVE_VO_SECONDS=7.5`,
`CARTOON_NUM_SHOTS=2`). Hard constraint: **do not regress `cartoon`.**

## Requirements (from Yoav)

- New tab `yt-cartoon` with 4 new input columns inserted after ZapCap (F).
- **Tone** (G): per-row dropdown — current calm style vs new engaging style.
- **Cap Position** (H): relative-nudge dropdown shifting ZapCap caption height.
- **CTA Position** (I): relative-nudge dropdown shifting CTA-pill height.
- **Vid Length** (J): cap dropdown — up to 10s / 15s / 20s. Shots + VO scale to
  fill. **2 videos** on the 10s bucket, **1 video** on 15s and 20s (cost/time).
- Narration: "much more lively, engaging, clickable without violating policy,
  faster, more alive."

## Column map (read by HEADER NAME, positional fallback) — Yoav-confirmed

```
A Country | B Vertical | C Article | D Manual Image | E Voice Over | F ZapCap |
G Tone | H Cap Position | I CTA Position | J Vid Length |
K Change Size | L Script Pattern | M CTA | N CTA Text | O Open Comments |
P Ready Video 1 | Q Ready Video 2
```

Positional fallback indices: country 1, vertical 2, article 3, manualImage 4,
voiceOver 5, zapcap 6, tone 7, capPosition 8, ctaPosition 9, vidLength 10,
aspectRatio 11, scriptPattern 12, ctaEnabled 13, ctaText 14, openComments 15,
readyVideo1 16, readyVideo2 17. lastInputCol 15.

Per the council + the avatar-tab precedent, the Apps Script reader resolves
every column by header name first (`_colForHeaders`) so the user can keep
inserting/moving columns without silently corrupting reads.

## Vid Length → render plan (the load-bearing math)

Seedance only generates 4/8/12s clips, but the Rendi concat **trims** each clip
to an arbitrary `per_clip_seconds` and forces the total via `-t
total_video_seconds`. So any exact target is reachable by generating the
cheapest legal clip and trimming down. Captions are auto-timed by ZapCap from
the audio (no manual sync), so length scaling has no caption-drift risk.

| Vid Length        | Videos | Shots | Per-clip (trim) | Seedance gen | Total | VO words | Max effective VO |
|-------------------|--------|-------|-----------------|--------------|-------|----------|------------------|
| up to 10s (blank) | 2      | 3     | ~3.33s          | 4s x3        | 10.0s | ~14      | 9.5s             |
| up to 15s         | 1      | 4     | ~3.75s          | 4s x4        | 15.0s | ~22      | 14.5s            |
| up to 20s         | 1      | 5     | 4.0s            | 4s x5        | 20.0s | ~30      | 19.5s            |

- All clips generated at the cheapest 4s tier (zero/low trim waste, max visual
  variety for retention).
- A pure `plan_shot_durations(target_seconds)` returns `(num_shots,
  per_clip_seconds[], vo_word_budget, max_effective_vo)`. Cartoon stays the
  special case `[4,4]` (untouched).
- `compute_atempo` gains an optional `max_effective` param (defaults to the
  existing 7.5 constant → cartoon byte-identical).
- VO word budget is clamped to the per-bucket stitched length so TTS can't
  overrun; the existing shorten-and-retry path is reused per bucket.

## Tone → prompt registry

- `tone` resolves through a small dict: `{calm/current → CARTOON_PLANNER_PROMPT
  (today's calm prompt), engaging/lively → YT_CARTOON_ENGAGING_PROMPT}`.
- Blank Tone default: **engaging** (this tab exists for the new style) — FLAG
  for Yoav confirm.
- Both prompts are admin-editable settings, mirroring `cartoon_planner_prompt`.
- Registry (not a binary) so adding angles later (curiosity / problem-agitate /
  testimonial) is a config edit, per the Expansionist.

### Engaging prompt — compliance guardrails (essential, per council)

Clickable via legitimate technique: one concrete specific detail from the
article, direct "you", a real question, a surprising-but-true fact. Fast,
punchy, short sentences.

HARD BANS (resolve the clickable-vs-policy tension inside the prompt):
- No health/medical claims or cures.
- No guaranteed-money / income / returns claims.
- No fake urgency ("act now", "limited time", "today only").
- No fear-mongering, no shock-bait ("doctors hate", "one weird trick", "you
  won't believe", "what happens next").
- No sensational superlatives / absolute promises.
- **Fact-faithfulness: use ONLY facts supported by the article; never invent
  statistics, names, prices, or claims.** (The credibility + policy landmine.)
- Keep existing brand-safety: no real brands/logos/plates, no real people, no
  legible on-screen text.

## Position nudges

- **Cap Position** → offsets `ZapCapStyleOptions.top` (default 70, or 30 when
  CTA on). Much Higher/Higher/Default/Lower/Much Lower → top offset
  -16/-8/0/+8/+16 pts (lower top% = higher on screen). Clamp [5, 90].
- **CTA Position** → offsets `cartoon_cta` bottom-margin frac (default 0.19).
  Higher = larger margin (pill moves up). Steps +-0.06 / +-0.03. Clamp
  [0.05, 0.40]. Renderer gains a `bottom_margin_frac` override param
  (default = current constant).
- Both blank = today's default. Defensive label→offset map; unknown → 0.

## Architecture (chosen)

New `YtCartoonRow` (models/row.py) + new `row_processor_yt_cartoon.py` that
REUSES the existing module-level pure helpers (`generate_cartoon_plan` with a
tone-selected prompt, `compute_atempo`, `shorten_voiceover`,
`render_cartoon_cta_overlay_bytes`, ZapCap, Rendi `concat_clips_with_audio`).
The cartoon orchestration closure is NOT touched. New tab constant
`TAB_YT_CARTOON = "yt_cartoon"` threaded through queue, runner (dispatch +
timeout + `_tab_for_row`), and routes (`YtCartoonRowIn` + builder).

### Files to touch

Backend: `models/row.py`, `orchestrator/row_processor_yt_cartoon.py` (new),
`orchestrator/queue.py`, `orchestrator/runner.py`, `routes/jobs.py`,
`pipeline/cartoon_prompt.py` (tone param + duration planner) or new
`pipeline/yt_cartoon_plan.py`, `pipeline/cartoon_cta.py` (margin override),
`orchestrator/runtime_settings.py` (engaging prompt setting + timeout key).

Apps Script: `Code.gs` — `TAB_YT_CARTOON`, `YT_CARTOON_COLS`, tab detection
(`yt-cartoon`/`yt cartoon` BEFORE `cartoon` since it contains "cartoon"),
`_readYtCartoonRow` (header-name reads), `_validateYtCartoon`, dispatch
(`rows_yt_cartoon`), dropdowns for Tone / Cap Position / CTA Position /
Vid Length.

Tests: golden test on cartoon path (regression firewall) +
`plan_shot_durations` unit tests + payload round-trip.

## Rejected alternatives

1. **Extend `CartoonRow` + `process_cartoon_row` with optional fields.**
   Rejected: threads variable geometry through the fragile flat-8s closure;
   each hard-won fix gains two meanings; high regression risk on a live tab.
2. **Full generic "duration engine" + arbitrary lengths now (Expansionist).**
   Deferred: the pure `plan_shot_durations` already leaves the door open for
   arbitrary lengths/A-B testing later, without gold-plating scope today.

## Security & safety (rule 13)

- Ad-policy compliance is the headline risk — see guardrails above; the prompt
  bans the violating patterns and enforces fact-faithfulness.
- Input validation: Vid Length / Tone / positions coerced defensively
  (Apps Script + server), never 400 the batch (matches avatar enum coercion).
- No new secrets, no new external vendor, no new attack surface. Auth path
  unchanged (Google OAuth ID token + allowlist).
- Cost: 10s ~1.5x today's cartoon; 15s/20s cheaper per row (single video).
  No new paid service. Per-shot graceful degradation kept so a single failed
  shot doesn't kill a 15/20s single-video row.

## Post-launch fixes (2026-06-17, first live run job-316c46f420f2371b)

1. **Ready Video write-back skipped.** ``batch_write_video_urls`` had no
   ``yt_cartoon`` branch in its positional_fallback chain → ``else None`` →
   ``skip_unknown_tab_type`` → videos never written to P/Q. Added
   ``_YtCartoonCols`` (ready_video_start = P / col 15) + wired the write,
   read-processed, and header-rows maps in ``adapters/sheets.py``.
2. **Dead air on long videos.** Video was forced to the full bucket length
   while the VO was sized at the cartoon's slow 1.5 wps, so a fast engaging
   delivery left ~10s of silence on a 20s clip. Fix: video length now tracks
   the measured VO (``fit_video_to_vo``, capped at bucket, floored 6s) and the
   word budget rose to 2.3 wps (~45 words on 20s vs 29). Engaging prompt
   reworded to a 1-3 sentence script that fills the window.

## Open questions for Yoav

1. Blank-Tone default = engaging (this tab's purpose)? Or calm?
2. Bucket table (shots / words / videos) acceptable as the v1 default?
3. Position nudge step sizes acceptable (cap +-8/16 pts, CTA +-0.03/0.06)?
4. Platform = paid native (Taboola/Outbrain)? Sets the compliance ceiling.
