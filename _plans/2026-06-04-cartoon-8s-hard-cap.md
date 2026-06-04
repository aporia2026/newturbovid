# Cartoon: hard 8s video + VO that always fits, never truncated

Date: 2026-06-04
Status: proposed (awaiting approval)
Owner: Yoav
Trigger: refs/v1.mp4 — 11s video, VO cut mid-sentence ("…fifty thousand re-")

## What's wrong today

Looking at `src/bulkvid/orchestrator/row_processor_cartoon.py` + `src/bulkvid/pipeline/cartoon_prompt.py`:

1. Planner targets `CARTOON_TARGET_WORDS = 13`, max 15. With Gemini TTS at the
   slow end (~1.5 wps observed, per the comment at cartoon_prompt.py:60),
   15 words ≈ 10s raw → 7.7s effective after 1.3x speedup.
2. Effective VO > `LONG_AUDIO_THRESHOLD_SECONDS = 7.5s` flips the row processor
   into "long_audio" mode: bumps the last shot to Seedance 8s and extends the
   video up to `TARGET_VIDEO_HARD_MAX_SECONDS = 11.0s`.
3. The concat step is called with `total_video_seconds=target_video_seconds`
   (max 11s). Rendi enforces that length on the OUTPUT. If the actual TTS
   came out longer than expected (model variance, slow delivery), Rendi cuts
   the audio mid-word at the 11s mark.

Net: video is 11s (already over the user's 8s ceiling), AND the VO is still
truncated mid-sentence because the planner can produce a >11s VO when the TTS
goes slow.

## Goals (from Yoav, this session)

1. Video duration is **always exactly 8.0s**. No 11s long-audio mode.
2. The full VO fits inside the video with no audio truncation. Not one syllable cut.
3. When the natural VO would be too long, we **regenerate it shorter** (one extra
   TTS call). We do NOT truncate, we do NOT speed up further, we do NOT fail
   silently.

## Approach

### Change 1: Tighten the planner

`src/bulkvid/pipeline/cartoon_prompt.py`:
- `CARTOON_TARGET_WORDS`: 13 → **10**
- `CARTOON_MIN_WORDS`: 11 → **8**
- `CARTOON_MAX_WORDS`: 15 → **12**

Rationale: at the slow end (1.5 wps), 12 words = 8s raw → 6.15s effective
after 1.3x speedup → 6.65s natural target (+ 0.5s dwell) → fits in 8s with
margin. At the fast end (3.5 wps), 12 words = 3.4s raw → 2.6s effective. Lower
floor is fine; we hard-pad to 8s either way (see Change 3).

Update the `CARTOON_PLANNER_PROMPT_DEFAULT` template variables that reference
these counts (the prompt already uses `{target_words}`, `{min_words}`,
`{max_words}` placeholders — no template edit needed, just the constants).

### Change 2: Shorten-VO LLM helper (new)

New function: `shorten_voiceover(client, *, text, language, target_words) -> ShortenResult`

- One `gpt-5.4-mini` chat call, JSON mode for robust parsing.
- System prompt: "Rewrite the user's voiceover line in {language}. Keep the
  meaning. End at a clean sentence boundary. Target ≤ {target_words} words.
  Return JSON {voiceover: '...'}".
- Returns `{ voiceover: str, cost_usd: float }`.
- Defensive parse: on failure return the original text unchanged (caller
  decides how to handle).

Why a dedicated call, not regenerate the whole plan: we want to keep the
chosen shots, style, character continuity. Only the VO line needs rewriting.

### Change 3: Row processor — strict 8s hard cap + regenerate loop

`src/bulkvid/orchestrator/row_processor_cartoon.py`:

Remove:
- `SEEDANCE_DURATION_LONG` (no more 8s last shot)
- `TARGET_VIDEO_MIN_SECONDS`, `TARGET_VIDEO_HARD_MAX_SECONDS`
- `LONG_AUDIO_THRESHOLD_SECONDS`
- The entire `long_audio` branch in `_build_idea`.

Keep + change:
- `TARGET_VIDEO_SECONDS = 8.0` (was `TARGET_VIDEO_MAX_SECONDS = 8.0`; rename
  to reflect "this IS the duration, not a max").
- `VO_TAIL_SECONDS`: 0.8 → **0.5** (tighter dwell; user wants snug fit).
- `MAX_EFFECTIVE_VO_SECONDS = TARGET_VIDEO_SECONDS - VO_TAIL_SECONDS = 7.5`.
- All shots use `SEEDANCE_DURATION_SHORT = 4` (with `CARTOON_NUM_SHOTS = 2`,
  total clip footage = 8s, matching the target exactly).
- `per_clip_seconds = [4.0, 4.0]` always.
- `total_video_seconds = TARGET_VIDEO_SECONDS = 8.0` always.

New VO-fit loop:

```
tts = await synthesize(idea.voiceover)
effective = tts.duration_seconds / SPEECH_ATEMPO
if effective > MAX_EFFECTIVE_VO_SECONDS:
    # Try once with a tighter target.
    shorter = await shorten_voiceover(
        openai, text=idea.voiceover, language=lang.language,
        target_words=max(6, planner_target - 3),
    )
    tts = await synthesize(shorter.voiceover)
    effective = tts.duration_seconds / SPEECH_ATEMPO
    if effective > MAX_EFFECTIVE_VO_SECONDS:
        # Give up on this idea — the OTHER idea may still ship.
        # Log, return None from _build_idea (existing graceful-degrade path).
        _log.warning("cartoon_vo_too_long_after_retry", ...)
        return None
```

Net: at most ONE extra TTS + one extra OpenAI call per affected idea. The
existing "one idea fails → other still ships" guard catches the rare case
where both ideas fail to fit.

### Change 4: Cost accounting

`_Costs` gets nothing new (the extra `shorten_voiceover` lands in
`costs.plan`; the extra TTS lands in `costs.tts`). Both are tiny per-row
costs — under $0.001 of OpenAI and ~$0.0005 of TTS for a 10-word retry.
No new budget flag needed.

## Alternatives rejected

1. **Speed up audio dynamically** (1.3 → 1.45+x when long). Voice quality
   degrades quickly past 1.3; produces a rushed, cartoonish delivery; user
   explicitly chose "regenerate" over "speed up".
2. **Truncate VO text deterministically before second TTS**. Risks ending
   mid-thought. The whole point of "no cutting" is to never produce a
   half-thought, even silently. LLM rewrite preserves meaning.
3. **Just hard-cap planner words tighter (e.g. max 10) without regenerate**.
   Easier, but a 10-word slow VO still hits ~6.7s → close to the line; one
   bad slot still truncates. Regenerate is the durable fix.
4. **Variable video length (4-8s band) following the VO**. User chose hard
   ceiling exact 8s.

## Security & safety (Rule 13)

- `shorten_voiceover` reuses the same OpenAI client, same auth path. No new
  external surface.
- No PII flows through this code path that wasn't already there.
- The retried VO still passes through `_enforce_word_cap` in the planner's
  shape, so it can't exceed the per-row word cap. (Adds a small assertion in
  the loop so a misbehaving shortener can't return something longer than the
  original.)
- Sensitive-apparel safety context is **not** re-applied in the shortener.
  That's intentional: shortening operates on an already-vetted VO, doesn't
  invent new content, just compresses. Flagged here so a future reader knows
  this is deliberate, not an oversight.

## Observability (Rule 14)

Existing `cartoon_vo_sized` log line keeps firing — but the fields change
(no more `long_audio`, no `clamp` — those concepts are gone). Replace with:

- `cartoon_vo_sized`: `idea`, `vo_raw_seconds`, `vo_effective_seconds`,
  `natural_target_seconds`, `target_video_seconds=8.0`, `fits=true|false`.
- `cartoon_vo_shorten_attempted`: `idea`, `original_words`, `target_words`,
  `original_effective`, `new_effective`, `new_words`, `fits_now`.
- `cartoon_vo_too_long_after_retry`: `idea`, `original_effective`,
  `retry_effective`, `dropped=true`. Triggers the existing
  `cartoon_idea_failed` path so the other idea still ships.

Plus the cost breakdown in `metadata` already covers the extra calls.

## Settings audit (Rule 15)

These are tunables that an admin might reasonably want to flip:

- `CARTOON_TARGET_WORDS` / `MIN` / `MAX`: candidates for a settings entry
  later. NOT exposed in this change — the values are derived from observed
  TTS rates and the 8s ceiling, not preferences. If the user wants a
  different ceiling we change `TARGET_VIDEO_SECONDS` and re-derive these.
- `TARGET_VIDEO_SECONDS = 8.0`: hardcoded for now. If a setting is desired
  later, it'd live alongside `cartoon_planner_prompt` in `runtime_settings.py`.
- Flagging that no new setting lands in this change to confirm the audit ran.

## Testing (Rule 18)

`tests/unit/test_cartoon_prompt.py`:
- New `test_shorten_voiceover_returns_shorter_text` — happy path.
- New `test_shorten_voiceover_falls_back_on_bad_json` — defensive parse.
- New `test_shorten_voiceover_never_lengthens` — assertion: returned VO
  word count ≤ original.

`tests/unit/test_row_processor_cartoon.py` (existing file — extend):
- `test_video_always_8s_when_short_vo` — VO is 3s effective; processor still
  builds an 8s video; per_clip_seconds = [4.0, 4.0]; total_video_seconds = 8.0.
- `test_video_always_8s_when_normal_vo` — VO is 6s effective; same.
- `test_vo_too_long_triggers_shorten_then_fits` — VO returns 9s the first
  time, 6s after shorten; idea ships successfully.
- `test_vo_too_long_after_retry_drops_idea` — both attempts return 9s;
  `_build_idea` returns None; row's other idea still ships.
- `test_no_long_audio_mode` — confirms `seedance_durations` is always
  `[4, 4]` (no 8s last shot anywhere).

All existing cartoon tests must stay green. The full suite (currently ~517
tests excluding the unrelated 5 sensitive-apparel failures) must stay green
or improve.

Manual smoke (cannot automate locally — vendor calls):
- Submit a cartoon row with a long article. Confirm the produced video is
  exactly 8.0s by `ffprobe`. Listen: VO should finish cleanly with brief
  trailing silence, never mid-word.

## Out of scope (flagged, not done here)

- Surfacing TARGET_VIDEO_SECONDS, CARTOON_TARGET_WORDS, etc. as admin
  settings. Add only if you decide later you want to tune them without code
  changes.
- The 5 pre-existing test failures in `test_routes_admin_settings.py`
  (sensitive-apparel-safeguard workstream). Tracked separately.
- Cold-start 500 on submit immediately after PA reload (the issue from earlier
  this session). Tracked separately — adds a warmup ping after deploy.

## Rollout

1. Implement Change 1 (planner constants + tests).
2. Implement Change 2 (`shorten_voiceover` helper + tests).
3. Implement Change 3 (row processor surgery + tests). Largest change; do last.
4. Run full test suite; in-scope tests green.
5. Commit + push.
6. Deploy via PA (`git pull` + reload). No Apps Script change this round.
7. Submit one cartoon row and `ffprobe` the result. Expect: duration=8.0s.
   Listen: VO finishes cleanly.

## Open questions

None blocking.
