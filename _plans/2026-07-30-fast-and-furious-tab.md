# fast-and-furious tab

Date: 2026-07-30
Status: APPROVED — implementing on the real prod base (hf/main)
Author: Claude + Yoav
Branch / worktree: `fast-and-furious-v2` (git worktree off `hf/main`)

## Goal

A new `fast-and-furious` Sheet tab: built on the same columns and N-video layout
as the existing `google-simple-motion` tab, but with **TikTok/Gen-Z narration**
and the **`use this script:` Open Comments override**.

- Each row produces **N different Gen-Z creative variations** (Number of Videos,
  1-4), each its own lively/fast/modern spoken script. This is the distinction
  from google-simple-motion (which shares ONE fixed "learn more" script across N
  sizes). Confirmed with Yoav: "N different creative variations."
- Video `i` uses **Change Size i** (blank → 9:16) and lands in **Ready Video i**
  (cols Q-T). Manual Image 1/2 are **shared** by every variation as shot 1 / shot
  2; a blank cell generates a realistic scene per variation.
- `use this script: <text>` in Open Comments → every variation speaks that exact
  operator text verbatim, any language.

## Column layout (identical to google-simple-motion)

```
A Country | B Vertical | C Article | D Manual Image 1 | E Manual Image 2 |
F Number of Videos (1-4) | G Voice Over | H ZapCap |
I Change Size 1 | J Change Size 2 | K Change Size 3 | L Change Size 4 |
M Script Pattern | N CTA | O CTA Text | P Open Comments |
Q Ready Video 1 | R Ready Video 2 | S Ready Video 3 | T Ready Video 4
```

## Architecture — a sibling of google-simple-motion (reuse, don't fork)

Built as a true sibling of `row_processor_google_simple_motion`, reusing the
shared infrastructure it added:

- **`build_pinned_cartoon_video`** does the per-video work. Each variation is
  routed through it with `fixed_shots=True`: it speaks the given script verbatim
  at natural pace and **sizes the video to the voiceover** (`total = max(floor,
  vo + tail)`, no upper cap) — that is the "no dead-air tail" behaviour, for free,
  and the same code path the `use this script:` override uses.
- `generate_cartoon_plan(num_ideas=N, num_shots=2, planner_prompt_key=<fast-
  furious>)` produces N independent Gen-Z ideas (voiceover + 2 realistic scenes).
- Manual Image 1/2 are resolved (download + re-upload) **once** and shared across
  variations; a blank/failed cell degrades that shot to a generated scene.
- Slot-aligned output (`[url or "" for url in results]`) so a failed variation
  leaves its Ready Video cell empty without shifting the others.

`process_google_simple_motion_row` / `process_simple_motion_row` are untouched.

### Why this base

The original attempt was built off `origin/main`, which is 7 commits behind prod
(`hf/main`) — it did NOT contain `google-simple-motion`, `image_resize`, per-sheet
KIE key routing, or the extended pinned builder. That version reinvented the
N-video pipeline and duplicated `audio_gap` etc. This rebuild sits on `hf/main`
and reuses the real code. (Git reconciliation of origin↔hf is handled as part of
the deploy — see Deploy.)

## Tunables (fast-and-furious only)

- `FF_TARGET_WORDS=17` / `FF_MIN_WORDS=14` / `FF_MAX_WORDS=22` — a punchy Gen-Z
  line that fills ~8s. The planner's word cap bounds video length.
- `FF_MIN_VIDEO_SECONDS=6.0` — low floor; the video otherwise follows the VO.
- `FF_NUM_SHOTS=2`, `FF_MAX_VIDEOS=4`.
- Row timeout 1800s (same heavy N-video shape as google-simple-motion).

## Files

New: `orchestrator/row_processor_fast_furious.py`, `tests/unit/test_fast_furious.py`,
this plan. Wired (mirroring google-simple-motion) in: `models/row.py`,
`runtime_settings.py` (Gen-Z planner prompt + timeout setting),
`orchestrator/queue.py`, `orchestrator/runner.py`, `routes/jobs.py`,
`adapters/sheets.py`, `apps_script/Code.gs`.

## Security & compliance (rule 13)

Paid native over sensitive verticals (weight-loss, finance, property): the Gen-Z
planner prompt keeps a HARD ad-compliance block (no health/money claims, no fake
urgency, no fear-bait, fact-faithful). Realistic photographic scenes, generic
people, no real brands/figures/legible text. The verbatim override bypasses
generation fences by design (operator-approved copy) and is logged/flagged
(`script_used_override`, `script_override_oversize`) exactly as the other tabs.

## Cost (rule 8)

~N× a single-video row (each variation = image-gen + Seedance + TTS + optional
ZapCap). Manual-image re-upload shared once. At N=4, ~4× — deliberate.

## Testing (rule 18)

Unit: payload round-trip, dispatch routing, N-video slot-aligned output,
per-variation aspect, Gen-Z prompt selection, override → every variation speaks
the operator text, settings registered. Stubs `build_pinned_cartoon_video`
(exercised on its own suite). Full unit suite green; ruff + mypy clean; Code.gs
node --check. One real render watched before prod is trusted.

## Deploy (rule 19)

Prod is `hf/main`; it had diverged ahead of `origin/main` (pre-existing debt).
Plan: reconcile origin to prod (revert the earlier stray PR #26, merge prod's
commits into origin, add this work), deploy the same commit to BOTH `origin/main`
and `hf/main` so they end IN SYNC, then `clasp push` Code.gs (after a `clasp
pull`/diff reconcile). Additive change — cannot break existing tabs; rollback =
revert + re-push.
