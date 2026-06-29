# Pinned verbatim script via Open Comments — all tabs

Date: 2026-06-29
Owner: Yoav (request relayed from manager)
Status: approved 2026-06-29 (pressure-tested by LLM Council)

## Decisions (Yoav, 2026-06-29)

- **Compliance posture:** verbatim + audit log. Speak the pinned text exactly;
  record every override (row + text hash + `script_used_override`) for audit. No
  silent rewrite, no moderation pass in v1.
- **Rollout:** ALL tabs in one release. Phase 1 (5 voiceover tabs) and Phase 2
  (cartoon-family verbatim + auto-fit) ship together, not staged.
- **Defaults confirmed:** length ceiling ~60 words / ~30s (flag, never truncate);
  cartoon OVERRIDE => ONE video, duration driven by the measured TTS audio.

## Goal (verbatim intent)

The bulk team types `use this script: <text>` into the **Open Comments** cell and
the video speaks **exactly** that text. Manager's words (translated): "add the
ability that if I write in Open Comment a script, it uses that exact script…
applies for all tabs. Script Pattern can stay for a general English hint that
adapts to the article — what I need is just the first part."

So: an explicit, reliable, operator-pinned voiceover, on every tab that makes a
spoken video. `Script Pattern` behaviour is unchanged.

## What already exists (this feature is ~80% built)

- `pipeline/open_comments.py` classifies the cell into NONE/TONE/DIRECTIVE/
  OVERRIDE/MIXED via an LLM (`gpt-5.4-mini`).
- On `OVERRIDE`, `pipeline/script_gen.py` (line 244) short-circuits and speaks
  the operator's text **verbatim, no LLM call, zero cost**.
- That path is wired into the 5 "real voiceover" tabs: **simple, image_vo,
  simple_x4, 4images, avatar**.

### The real gaps vs. the manager's ask

1. **It's an LLM guess, not a guarantee.** Whether the text is used verbatim
   depends on the classifier deciding "this looks like 20+ words of prose." The
   manager's Dutch line is ~23 words — right on the threshold. The literal
   `use this script:` prefix the team is already typing is **not a recognised
   trigger** anywhere. Failure modes: misclassified as DIRECTIVE/MIXED and
   rewritten; or the prefix left inside the spoken text; or two identical-looking
   rows behaving differently.
2. **The 3 cartoon-family tabs never speak it verbatim.** `cartoon`,
   `yt-cartoon`, `simple-motion` pass the override to a multi-shot planner as
   `PREFERRED VOICEOVER (use or adapt for at least one idea)`
   (`pipeline/cartoon_prompt.py:207`). It gets reshaped across shots.
3. **`text_on_img` ignores Open Comments** by design (it outputs an image, not a
   video). Correctly out of scope.

## Verified facts (checked in code, not assumed)

- **ZapCap captions the AUDIO, not the cell.** `caption_video(video_bytes=…)`
  (`adapters/zapcap.py:286`) transcribes the rendered video; the spoken text is
  `script.script` (`row_processor_simple.py:192`). => A correctly-stripped marker
  cannot leak into captions. The leak risk reduces entirely to "strip the marker
  before TTS."
- **OVERRIDE bypasses the safety/compliance block.** `script_gen.py` returns at
  line 244 (OVERRIDE) *before* the sensitive-apparel safety block is fetched and
  appended (lines 264-304). Verbatim operator text is spoken with **no content
  review**. This is inherent to "speak exactly this" — the LLM compliance fences
  only constrain *generation*, not pasted text. It is a posture decision (see
  Security).

## Locked decisions (product owner, 2026-06-29)

1. Verbatim on the cartoon tabs too — auto-fit the animation to the script.
2. Trigger = explicit `use this script:` marker, stripped deterministically
   **before** any LLM call, PLUS keep today's LLM auto-detect of a bare pasted
   script (no marker).

## Build progress (2026-06-29)

- **Phase 1 — DONE, tested, zero regressions.** Marker detector
  (`detect_pinned_script`) + classifier short-circuit + uniform override audit
  log (hash + words + source) + `override_oversize` flag threaded through
  `script_gen` and the 5 script-tab processors' metadata. Full unit suite green
  (~1,200 tests). Dropped the bare `script` marker (false-positive risk:
  "script should be upbeat" is a directive) — only the explicit `use …` family
  triggers.
- **Phase 2 math — DONE, tested.** `plan_pinned_shots()` pure helper in
  `yt_cartoon.py`: audio drives length (no upper cap on the video — capping
  truncates the operator's words), shots scale 2→8, Seedance tiers always legal.
- **Phase 2 wiring — DONE, tested.** A single shared builder
  `orchestrator/pinned_cartoon.build_pinned_cartoon_video` holds the surgery; the
  3 animated processors keep their generate-narration paths byte-identical and
  only branch at the plan params + the build step. On OVERRIDE: skip
  narration-gen + the shorten/8s-cap path, TTS the pinned script at natural pace
  (atempo 1.0), fit shots via `plan_pinned_shots` (variable for cartoon/
  yt-cartoon) or keep the operator's 2 manual images (fixed, for simple-motion),
  produce ONE video. Builder unit tests + one routing test per processor; full
  unit suite green, zero regressions. ruff + mypy clean on all touched files.

- **Discoverability (Apps Script) — DONE.** No Code.gs/Sidebar.html change was
  *required* (Open Comments is read raw + sent on every tab; the marker is parsed
  server-side). Added an optional `applyOpenCommentsTips()` menu action
  (`Aporia Bulk Video > Add "use this script" tips`) that writes a header NOTE on
  the Open Comments column of every video tab (skips text-on-img) so a new
  operator discovers the convention without being told. Mirrors
  `applySizeDropdowns`; idempotent; node `--check` clean.

## Operator validation needed before deploy (Yoav)

Render ONE real cartoon row with `use this script: <a 2-3 sentence script>` and
watch it. The auto-fit is mechanically correct, but a fixed script over animated
shots can look visually thin (council's standing warning). Deploy is the manual
`git push hf main` — do that only after the eyeball check passes.

## Chosen approach

### Phase 1 — deterministic marker on the 5 script tabs (ship first)

The marker is **content arriving through a signal channel** (First Principles'
reframe). So it gets its own small door rather than logic buried in the
classifier — but no new sheet column (operator UX stays identical).

- New pure helper `detect_pinned_script(text) -> str | None` in
  `pipeline/open_comments.py`:
  - Case-insensitive, whitespace-tolerant, punctuation-tolerant match of a small
    marker set at the **start** of the cell: `use this script:`, `use script:`,
    `script:`, with `:`/`-`/`–`/`=` separators and an optional trailing space.
    Mid-string matches do NOT trigger (prefix only).
  - Returns the remainder with the marker stripped; `None` if no marker or the
    remainder is empty after stripping.
- `classify_open_comments()` calls it first: on a hit, return
  `OpenCommentsAnalysis(mode=OVERRIDE, override_script=<stripped>, cost_usd=0.0)`
  with **no network call**. Otherwise fall through to today's LLM classifier
  (preserves the locked bare-paste auto-detect).
- The 5 script tabs already honor OVERRIDE => no processor changes needed beyond
  this single seam.
- **Length ceiling** (cost + ad-format guardrail): if the pinned script exceeds
  a sane cap (proposed ~60 words / ~30s of speech), keep it but set a metadata
  flag `script_override_oversize=True` so it's visible; do not silently ship a
  90-second "Short." (Hard truncation rejected — it would mangle the operator's
  approved copy mid-sentence.)

### Phase 2 — cartoon-family verbatim (separate PR)

The genuinely hard part; do not let it block Phase 1.

- On OVERRIDE, bypass the narration half of `generate_cartoon_plan()`, force the
  pinned script as the single narration track, TTS it, measure real duration, and
  plan the shots to **cover that duration** (audio drives length).
- Resolve the two open questions per council:
  - **One video, not two.** Two videos from one fixed script is incoherent.
  - **Duration follows the audio, bounded by a guardrail.** The Vid Length cap
    governs *generated* length; it has no authority over human-supplied content,
    but a hard max still protects the ad slot and cost (same ceiling as Phase 1).
- Validate the result by eye before trusting auto-fit — a 23-word line stretched
  over a 20s multi-shot cartoon can look visually thin (Contrarian's warning).

## Edge cases to handle (from the council)

- Empty body after stripping the marker => treat as NONE (no override).
- Marker-only cell, marker mid-string (not a prefix) => no override.
- Smart-colon / autocorrect / trailing spaces / mixed case => still match.
- Operator writes the marker in another language => documented as NOT supported
  in v1; falls through to bare-paste auto-detect (acceptable, logged).
- Re-runs / edited cells: an edited pinned script re-TTSs and overwrites the
  Ready Video on reprocess. Call this out to operators; full idempotency/asset
  versioning is out of scope for v1 (noted as a follow-up).

## Security / safety / compliance (rule 13)

- **The bypass is real and gating.** Verbatim text skips every content fence on
  paid native ads (Taboola/Outbrain). Recommended posture: keep verbatim *truly*
  verbatim (rewriting it defeats the feature and the manager's intent), but
  **log + flag** every override — record `script_used_override=True`, a hash of
  the spoken text, and the row — so there is an audit trail and a human can spot
  bad copy. Optional hardening: a cheap moderation pass that **flags without
  rewriting**. **This needs Yoav's explicit decision before Phase 1 ships.**
- No new secrets, no new external surface, no PII. The marker path actually
  *reduces* attack surface (one fewer LLM call) and is fully deterministic.

## Cost (rule 8)

- No new paid service. The marker path **saves** one `gpt-5.4-mini` classifier
  call per pinned row (deterministic, zero-cost).
- Verbatim still pays existing TTS + ZapCap + render per row. The length ceiling
  caps the blast radius the council flagged (uncapped paste × 400 rows).

## Lazy-operator UX (rule 10)

- Make the path **visible**: surface "verbatim script used" back to the operator
  (the metadata `script_used_override` already exists; expose it in the sidebar /
  status so they can SEE which rows were pinned vs generated). Silent inference is
  the Outsider's top complaint.
- Tolerant matching means the lazy operator's `USE THIS SCRIPT -`, trailing
  spaces, and autocorrect colons all still work.

## Alternatives rejected

- **Build an instruction grammar / named-script library now** (`use voice:`,
  `use music:`, `#approved-script-v3`, hash-dedupe). Unanimous council blind
  spot: gold-plating an unproven, not-yet-safe core; invents requirements the
  manager never asked for. Noted as a *future* direction; build none of it now.
- **A dedicated "Voiceover script" sheet column / "use exactly" checkbox.**
  Cleaner UX in the abstract, but adds a column to every tab and Apps Script
  payload, and the manager explicitly wants the Open Comments path. Revisit only
  if inference proves too confusing in practice.
- **Marker logic buried inside the classifier body.** Rejected for the pure
  `detect_pinned_script()` helper — same seam, but testable in isolation and not
  conflating "classify" with "this isn't a thing to classify."
- **Only improve the LLM classifier prompt to recognise the marker.** Rejected:
  still probabilistic, still costs a call, still can leak the prefix.

## Open questions for Yoav (need answers before/with Phase 1)

1. **Compliance posture:** verbatim + log/flag (recommended), or add a
   flag-only moderation pass? (Do not silently rewrite.)
2. **Length ceiling number:** ~60 words / ~30s ok, or different?
3. **Confirm Phase 2 cartoon = ONE video, audio-driven duration** (vs today's
   2-idea cartoon / Vid Length bucket).

## Files touched

Phase 1:
- `pipeline/open_comments.py` — `detect_pinned_script()` helper + early return.
- `pipeline/script_gen.py` — length-ceiling metadata flag on the OVERRIDE path.
- Tests: `tests/unit/test_open_comments.py` (+ marker cases), any classifier
  call-count assertion that the short-circuit breaks.

Phase 2 (separate PR):
- `pipeline/cartoon_prompt.py` / `generate_cartoon_plan` — OVERRIDE bypass +
  auto-fit.
- `row_processor_{cartoon,yt_cartoon,simple_motion}.py` — one-video, audio-driven
  branch on OVERRIDE.
- Tests for each.

## Test plan (Phase 1)

- Marker present (all variants) => OVERRIDE with exact stripped text, no LLM call.
- Marker mid-string / marker-only / empty-after-strip => NOT override.
- Bare paste with no marker => still auto-detects (locked behaviour intact).
- Oversize pinned script => override used + `script_override_oversize` flag set.
- Existing classifier tests still green; fix call-count assertions.
