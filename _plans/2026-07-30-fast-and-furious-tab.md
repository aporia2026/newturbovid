# fast-and-furious tab

Date: 2026-07-30
Status: APPROVED v2 (multi-video semantics locked with Yoav 2026-07-30) — implementing
Author: Claude + Yoav
Branch / worktree: `fast-and-furious` (git worktree, off `origin/main`)

## v2 pivot (2026-07-30) — MULTIPLE videos per row

The tab as Yoav actually built it has extra columns beyond google-simple-motion:
**Number of Videos** and **four Change Size** columns feeding **Ready Video 1-4**.
Confirmed with Yoav:

- Each row produces **N DIFFERENT creative variations** (N = "Number of Videos",
  1-4), each its own Gen-Z script + its own visuals — like the cartoon tab's
  multi-idea output, not a resize of one video.
- **Video i uses Change Size i** for its aspect ratio (blank → 9:16). Extra Change
  Size columns beyond N are ignored.
- **Manual Image 1/2 are shared by all N videos** (shot 1 / shot 2); a blank cell
  is auto-generated per video. Manual images are downloaded + re-uploaded ONCE and
  reused across the N videos (aspect-independent source).
- Every video keeps the TikTok/Gen-Z narration + no-dead-air (VO-driven length).
- Outputs land in Ready Video 1..N.

This is a genuinely different, bigger pipeline than simple-motion (N variations,
PER-VIDEO aspect ratio, N outputs), so it gets its OWN processor
(`row_processor_fast_furious.py`) that REUSES the shared helpers — exactly how
`simple-motion` is "a sibling of cartoon that reuses the shared helpers." The
`process_simple_motion_row` path is left byte-for-byte pristine.

**Cost (rule 8):** ~N× the paid API cost of a simple-motion row (each video = its
own image-gen + Seedance + TTS + optional ZapCap). At N=4 that's ~4× per row —
deliberate, confirmed with Yoav. Manual-image re-upload is shared (done once).

## Goal (original — kept for the narration + no-dead-air intent)

A new `fast-and-furious` tab: realistic-image, manual-image-or-generate videos,
with two differences from simple-motion:

1. **TikTok-style narration.** The voiceover is lively, fast, modern, Gen Z —
   punchy spoken language, hook-first — instead of the calm article-driven line
   the simple-motion tab writes. It adapts to each row's market language (not
   forced English slang).
2. **No dead-air tail.** Yoav's exact note: "same length as google-simple-motion,
   but without that ~3 seconds break in speech." Today the video is padded to a
   flat 8s while the VO only fills ~5s, leaving a silent tail. fast-and-furious
   ends the video with the voiceover (VO-driven length, capped at the 8s of
   footage) AND writes a longer line, so narration runs the full ~7-8s.

The Open-Comments "use this script: …" verbatim override is ALREADY built and
tab-agnostic (`detect_pinned_script` → verbatim TTS, any language, marker
stripped, audio-driven length). Because fast-and-furious reuses the simple-motion
processor, that requirement is satisfied for free — verified in code, not assumed.

## Resolved decisions (Yoav 2026-07-30)

1. **Pace:** energetic, natural fit — Gen Z wording + an upbeat/fast TTS delivery
   hint, sized to the ~8s window at a natural-to-slightly-fast pace. NOT chipmunk
   speed-up (atempo ceiling unchanged, protects voice quality).
2. **Geometry:** same as google-simple-motion (one ~8s video, two 4s shots,
   manual images D/E or generated, Ready Video 1), BUT no ~3s silent tail.
3. **Settings:** its own admin-editable planner prompt + row-timeout key, like
   every other tab.

## Architecture — a variant of simple-motion, NOT a fork (SSOT)

fast-and-furious is byte-for-byte identical to simple-motion in columns, geometry,
image resolution, CTA, ZapCap, and writeback. The ONLY real differences are the
narration prompt and the VO-length sizing. So we do NOT copy the 780-line
processor (that would duplicate the whole pipeline and rot). Instead:

- A distinct tab type `fast_furious` is wired end to end (Apps Script → queue →
  runner → sheets) exactly the way `yt-cartoon` and `simple-motion` were added.
- A distinct `FastFuriousRow` dataclass (same fields as `SimpleMotionRow`) so
  isinstance-based dispatch + per-tab timeout stay unambiguous.
- `process_simple_motion_row` gains keyword-only, defaulted knobs (planner prompt
  key/default, target/min/max words, VO fit ceiling, `end_with_voiceover`, `tab`
  label). Defaults reproduce today's simple-motion behaviour byte-for-byte, so
  the simple-motion tab is provably unchanged.
- A thin `process_fast_furious_row` wrapper calls it with the fast-furious knobs.

This mirrors how `generate_cartoon_plan` is already parameterized for yt-cartoon
(prompt-key + word budget) and how `concat_clips_with_audio` /
`image_to_video_fit` already take an optional `total_video_seconds` (the recent
"simple: end the video with the voiceover" commit, 72a12e2).

### The "no 3s break" mechanism (verified against the code)

- Today: `total_video_seconds = TARGET_VIDEO_SECONDS` (flat 8.0) → tail silence
  when the VO is shorter. `compute_atempo` already refuses to over-speed short
  VOs, so it can only shrink the gap, not close it.
- fast-and-furious: when `voice_over` and `end_with_voiceover`, set
  `total_video_seconds = min(effective_vo + tail, footage_seconds)` where footage
  is the 8s of two 4s clips. The video ends with the VO — zero dead air
  regardless of TTS-speed variance (which spans ~1.5-3.5 wps, so word count alone
  can't reliably fill exactly 8s). A longer word budget keeps the typical video
  ~7-8s, so it still "feels the same length" as google-simple-motion.
- `voice_over = No` rows keep the flat 8s (there is no VO to end on).
- The pinned "use this script:" path already audio-drives length via
  `build_pinned_cartoon_video`, so it has no dead air and is left untouched.

### Tunables (fast-and-furious only; simple-motion defaults unchanged)

- `FAST_FURIOUS_TARGET_WORDS = 17`, MIN `14`, MAX `22` — an energetic line that
  naturally reads ~7-8s. (simple-motion stays at 10 / 8 / 12.)
- `FAST_FURIOUS_MAX_EFFECTIVE_VO_SECONDS = 7.8` — lets the longer line fit inside
  the 8s footage without triggering shorten-and-retry, while staying < footage so
  the last frame never freezes.
- `FAST_FURIOUS_VO_TAIL_SECONDS = 0.3` — small breath after the last word;
  video length = min(effective + 0.3, 8.0).
- `end_with_voiceover = True`.

## Column map (identical to simple-motion)

```
A Country | B Vertical | C Article | D Manual Image 1 | E Manual Image 2 |
F Voice Over | G ZapCap | H Change Size | I Script Pattern | J CTA |
K CTA Text | L Open Comments | M Ready Video 1 | N Ready Video 2
```

Reuses `SIMPLE_MOTION_COLS`, `_readSimpleMotionRow`, `_validateSimpleMotion`.
Ready Video 1 = col M; Ready Video 2 left empty (one video per row).

## Files touched

Backend:
- `orchestrator/runtime_settings.py` — `FAST_FURIOUS_PLANNER_PROMPT_DEFAULT`,
  `SETTING_FAST_FURIOUS_PLANNER_PROMPT`, `SETTING_ROW_TIMEOUT_FAST_FURIOUS`,
  both registered in `SETTINGS_REGISTRY`.
- `models/row.py` — `FastFuriousRow` (same fields as `SimpleMotionRow`).
- `orchestrator/row_processor_simple_motion.py` — parameterize
  `process_simple_motion_row` (defaults = today's simple-motion); add the
  fast-furious constants + `process_fast_furious_row` wrapper.
- `orchestrator/queue.py` — `TAB_FAST_FURIOUS`, payload (de)serialize.
- `orchestrator/runner.py` — dispatch, timeout default + setting key,
  `_tab_for_row`.
- `routes/jobs.py` — `FastFuriousRowIn`, `rows_fast_furious`, builder, dispatch.
- `adapters/sheets.py` — header-rows, read_processed, writeback fallback all map
  `fast_furious` → `SIMPLE_MOTION_COLS`.

Apps Script (`Code.gs`):
- `TAB_FAST_FURIOUS`; add to `SEEDANCE_VIDEO_TABS`; detect
  `fast-and-furious` / `fast and furious` / `fast_and_furious`; cols / readRow /
  validate / payload dispatch all reuse the simple-motion handlers; add the tab to
  the `showActiveModels` video card.

Tests:
- `tests/unit/test_fast_furious.py` — payload round-trip; `process_fast_furious_row`
  routes to the shared processor with the fast-furious prompt + VO-driven length;
  pinned override still verbatim; simple-motion path proven unchanged (default
  args); Apps Script string presence smoke via existing node `--check` in CI.
- Extend `tests/unit/test_simple_motion.py` regression guard where relevant.

## Security & safety (rule 13)

- **Paid-native compliance is the real risk here.** The sheet's live verticals
  include Weight Loss Injections, Car Installments, Home Loans, Apartments — all
  sensitive on Taboola/Outbrain. A "make it TikTok / edgy" prompt pulls AGAINST
  ad compliance. The prompt therefore keeps a HARD compliance block (adapted from
  the yt-cartoon engaging prompt): no health/medical/weight-loss claims, no
  guaranteed money/returns, no fake urgency, no fear-mongering or shock-bait, no
  sensational superlatives, fact-faithful only. Energy comes from delivery +
  real detail, never from crossing these lines.
- Visual rules unchanged: `REALISTIC_STYLE` photographic scenes, generic/ordinary
  people only, never a real public figure, no real brands/logos/plates, no
  legible on-screen text. The shared sensitive-apparel safeguard still applies.
- The verbatim "use this script:" override still bypasses generation fences (by
  design — it speaks operator-approved copy) and is logged/flagged exactly as
  today (`script_used_override`, hash, oversize flag). No change to that posture.
- No new secrets, no new vendor, no new external surface. Manual image URLs are
  downloaded + re-uploaded through the existing size/timeout-bounded guard.

## Observability (rule 14)

- `metadata["tab"] = "fast_furious"` and `tab="fast_furious"` on the row_start /
  row_done / vo_sized logs so a fast-furious row is greppable and its VO-fill /
  video-length is visible. `pinned_video_seconds` already logged on override rows.
- New: log the computed VO-driven `video_seconds` per idea so "did the tail
  close?" is answerable from logs alone.

## Settings (rule 15)

- `fast-and-furious: planner prompt` (multiline) — tune the Gen Z tone without a
  redeploy. `Row timeout: fast-and-furious (seconds)` (default 1200, same shape as
  simple-motion). Both in the admin registry. Change Size + CTA + Voice Over +
  ZapCap dropdowns are the same operator controls simple-motion already exposes;
  `applySizeDropdowns` picks them up once the tab joins `SEEDANCE_VIDEO_TABS`.

## Deploy (rule 19)

- Backend: PR `fast-and-furious` → `main`; merge triggers the standard flow, then
  prod is the manual `git push hf main:main` (per memory `turbovid_deploy_flow`).
  Nothing pushed by hand in this session; no production-tracking branch touched.
- Apps Script: `Code.gs` change ships via `clasp push` — SEPARATE from git.
  Because live Apps Script can drift from the committed file (memory
  `turbovid_apps_script_sync_drift`), reconcile first: `clasp pull` / diff in the
  main checkout BEFORE `clasp push`, so the push can't clobber an un-committed live
  edit. Flag to Yoav before pushing.
- Rollback: revert the PR; `clasp push` the prior `Code.gs`. The new tab is
  additive — existing tabs are untouched, so a rollback can't break live batches.

## Testing (rule 18)

Unit level (pytest): payload round-trip, dispatch routing, VO-driven length math,
prompt selection, pinned-override still verbatim, simple-motion-unchanged guard.
Apps Script: node `--check` syntax (CI) + manual eyeball of one real row before
prod. Integration/E2E of the paid render pipeline stays a manual eyeball (paid
external I/O, no seam) — one real `fast-and-furious` row rendered and watched
before `git push hf`, same gate simple-motion used.

## Rejected alternatives

1. **Copy `row_processor_simple_motion.py` into a new file.** Rejected —
   duplicates the entire 780-line paid pipeline to change a prompt string; the
   two would drift. Parameterizing the shared processor is the SSOT move.
2. **Reuse the `simple_motion` tab_type and switch prompt by worksheet name.**
   Rejected — the processor never sees the worksheet name; threading it in just to
   pick a prompt is hacky and breaks the clean one-tab-type-per-tab pattern.
3. **Fill 8s with word count alone (no VO-driven length).** Rejected — TTS speed
   varies ~2.3x, so word count can't reliably close the tail; short rows would
   still have dead air, long rows would trigger shorten-and-retry / drops.
4. **Shorten the video hard to the VO with no larger word budget.** Rejected on
   its own — a 5s video reads as "shorter than google-simple-motion." Combined
   with the bigger budget it lands ~7-8s, which is what Yoav asked for.

## Open questions

None blocking. Confirm the Gen Z default prompt reads well after the first real
render (admin-editable, so tunable without a deploy).
