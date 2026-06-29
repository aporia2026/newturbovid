# Engaging auto-voiceovers on Gemini 3.1 Flash TTS

Date: 2026-06-22
Owner: Yoav
Status: approved, in progress

## Goal

Move the TTS stack off the pinned `gemini-2.5-flash-preview-tts` onto Google's
newest model and its new abilities, and make the system **automatically attach
an engaging voiceover** to each row — voice + delivery chosen from the row's
**vertical, article, and vibe (Open Comments)** — with "always engaging" as a
hard floor. Operator-facing: nothing new to learn. It just sounds livelier.

## What the user asked for (verbatim intent)

- "use the latest google tts… their latest models… newest abilities and voices."
- "voiceovers that are lively, punchy and engaging."
- "automatically attach voiceovers based on the vertical, article, vibe, but
  always make it engaging."
- Operator controls: automatic under the hood (no new sheet columns).
- Rollout: script-gen tabs first (simple, image_vo, simple_x4, four_images),
  cartoon planner tabs later. Model switch config-driven, default 3.1.

## Verified research (June 2026)

- Newest model: `gemini-3.1-flash-tts-preview` (public preview, Vertex AI +
  Gemini API, launched 2026-04-15). Same `generateContent` request shape as
  `gemini-2.5-flash-preview-tts`: `response_modalities=["audio"]` +
  `speech_config → voice_config → prebuilt_voice_config → voice_name`. Same
  output: raw 24 kHz / mono / 16-bit PCM. **Drop-in** on our existing call —
  WAV wrapper, PCM extractor, audio constants all stay valid.
- 30 prebuilt voices (we use 8; all 8 remain valid). Google does **not**
  publish a voice→character table on its docs (checked 3 of their pages); the
  AI-Studio descriptors are community-reported. So any "Puck = upbeat" claim is
  to be confirmed by ear, not asserted as documented fact.
- New ability: inline audio tags (`[excitedly]`, `[cheerfully]`, `[soft laugh]`
  …). Verified recipe for punchy/engaging delivery: be vivid + frame
  positively, never use flatness words ("calm", "no rush", "flat"); descriptive
  style prompt is the primary lever, tags secondary.
- Pricing: 3.1 Flash TTS is token-based — $1/M input tokens, $20/M output
  tokens, audio billed ~25 output tokens/sec → ~$0.03 per 60s VO. Our clips are
  short (8-20s) so well under a cent each; a ~1000-video batch ≈ a few dollars.
  The Flash tier is the cost-efficient choice for bulk; 2.5-pro is the premium
  escalation lever if a vertical needs richer style adherence.

Sources: ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-tts-preview;
docs.cloud.google.com/text-to-speech/docs/gemini-tts;
ai.google.dev/gemini-api/docs/speech-generation; blog.google Gemini 3.1 Flash
TTS; nemovideo.com Gemini 3.1 Flash TTS pricing.

## Chosen approach

The pipeline already runs `generate_script(vertical, article, country,
open_comments)` and emits a per-row `style_direction` consumed by TTS. That is
the hook. Three moves:

1. **Model swap, config-driven.** New `BULKVID_GEMINI_TTS_MODEL` setting,
   default `gemini-3.1-flash-tts-preview`. Rolling back to 2.5 (or escalating a
   run to `gemini-2.5-pro-tts`) is a one-line env change, no redeploy — the
   right safety net for a preview model whose quota may need a retune.

2. **Always-engaging delivery.** Rework `SCRIPT_SYSTEM_PROMPT_DEFAULT`: shift
   the tone guidance from "neutral, not emotional" to lively/punchy/engaging,
   reusing the proven `YT_CARTOON_ENGAGING_PROMPT_DEFAULT` pattern — energy
   fenced by the SAME compliance block (no CTAs, no urgency, no superlatives,
   no health/finance claims, sensitive-apparel rules stay verbatim). Bump the
   `DEFAULT_STYLE_DIRECTION` fallback from "warm friendly podcast host" to a
   bright, forward-moving, vocal-smile delivery so even the fallback path is
   engaging.

3. **Auto voice selection by content.** The script LLM (it already sees
   vertical/article/vibe) additionally returns a `voice` chosen from a curated
   lively menu injected into the prompt. `pick_voice` validates it against the
   voice pool and falls back to a livelier per-language default if the model
   omits or garbles it. Each script-gen synthesize call passes
   `voice=script.voice`.

### Hard constraint — no tags in the spoken script

ZapCap captions by transcribing the **audio** (`caption_video(video_bytes=…)`),
and `script.script` is what gets spoken. So bracket tags MUST NOT be embedded in
`script.script` or they'd be spoken/captioned literally. Energy lives only in
the `style_direction` prefix (sent to TTS as a soft instruction, never spoken,
never captioned) and in the engaging wording of the script itself. Audio tags
remain a future option scoped to the style prefix, after by-ear testing.

## Files touched

- `src/bulkvid/config.py` — add `BULKVID_GEMINI_TTS_MODEL`.
- `src/bulkvid/adapters/gemini_tts.py` — `DEFAULT_MODEL` → 3.1; expand voice
  pool + character map; livelier per-language defaults; token-based cost
  estimate; `build_client_from_settings` passes the configured model; expose a
  `LIVELY_VOICE_MENU_TEXT` for the prompt.
- `src/bulkvid/orchestrator/runtime_settings.py` — rework
  `SCRIPT_SYSTEM_PROMPT_DEFAULT` (engaging tone + compliance + `voice` output
  field + `{voice_menu}` placeholder).
- `src/bulkvid/pipeline/script_gen.py` — parse/validate `voice`, add to
  `ScriptResult`; engaging `DEFAULT_STYLE_DIRECTION`; inject `{voice_menu}`.
- `row_processor_{simple,image_vo,simple_x4,4images}.py` — thread
  `voice=script.voice` into `synthesize`.
- Tests: `test_gemini_tts.py`, `test_script_gen.py`, and any row-processor test
  asserting synthesize args.

Out of scope this pass (extend later): cartoon / yt-cartoon / simple-motion
planner path (yt-cartoon is already engaging); avatar TTS (narration is TikTok
Symphony, not Gemini) — but avatar's *script* still gets the engaging prompt.

## Alternatives rejected

- **Just bump the model** (no voice/tone work). Rejected: gets 3.1's naturalness
  but ignores the actual ask (engaging, content-aware voice).
- **Deterministic vertical→voice map.** Rejected: not article/vibe-aware and
  brittle on free-text vertical strings; LLM selection is on-pattern with the
  existing template-selector.
- **Rewrite onto Cloud Text-to-Speech GA API** (`gemini-2.5-flash-tts`).
  Rejected now: larger adapter rewrite, 3.1 is still preview there too, and we
  don't need MP3/OGG output.
- **Embed `[audio tags]` in the script.** Rejected: leaks into ZapCap captions.

## Security / safety / compliance

- These are paid native ads (Taboola/Outbrain) + Shorts. The engaging rework
  keeps every compliance fence from the current prompt verbatim; only the
  energy/tone guidance changes. Sensitive-apparel safety block path unchanged.
- Preview-model risk: behavior/quota can change. Mitigated by the config-driven
  model (instant rollback) and the existing RPM/concurrency caps (retune after
  first real 3.1 batch from semaphore-wait logs).
- No new secrets, no new external surface, no PII change.

## Open questions / follow-ups

- Confirm the lively voice shortlist **by ear** in AI Studio before trusting the
  community descriptors. Retune `VOICE_BY_LANGUAGE` after listening.
- **Deployment note:** the script prompt is admin-editable. If the prod admin
  panel holds a *customized* `simple_script_prompt` / `simple_x4_script_prompt`,
  the new engaging default + `voice` field will NOT apply until that stored
  prompt is updated or reset to default. Flag to Yoav.
- After the script-gen tabs are validated, port the engaging + voice pattern to
  the cartoon planners.
