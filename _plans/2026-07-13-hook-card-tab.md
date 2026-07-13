# Hook Card tab

Date: 2026-07-13
Status: approved scope, building

## Goal

A new `hook_card` tab that mass-produces the competitor's faceless short-video
style: a 9:16 vertical clip (~8s) where a slideshow of 1-5 background scenes
(each with a slow Ken Burns zoom) plays under a fixed black semi-transparent
rounded box holding one bold white centered "hook" headline, with royalty-free
background music. No voiceover.

Reference videos: `C:\Projects\ad-lib-dashboard\refs` (720x1280, 8s, music).

## Requirements (locked with operator)

Sheet columns (this exact order, matches the operator's tab):

| Col | Header | Role |
|-----|--------|------|
| A | Country | Market -> language / localization |
| B | Vertical | Topic context for AI copy + images |
| C | Article | Source URL for AI hook + AI scenes |
| D | Num of Images | Scene count, 1-5 (dropdown) |
| E | Text | Hook verbatim if filled, else AI-generated |
| F | Music | Track name to play; blank = random |
| G-K | Manual Image 1-5 | Background scenes if provided |
| L | Change Size | Output aspect (default 9:16) |
| M | Open Comments | Operator directives for AI copy |
| N | Ready Video | Output URL |

Behavior:
- Background: if any Manual Image (G-K) is filled, use those in order; else
  AI-generate `Num of Images` realistic photos from the article/topic
  (existing nano-banana pipeline). Motion = Ken Burns zoom, NOT per-clip AI
  animation.
- Hook text: column E verbatim if filled; else generate one hook line from the
  article in the market language.
- Box: lower third, same bundled Inter Bold + auto-fit + multiscript machinery
  the card renderer already uses.
- Music: bundled instrumental pool generated once with Suno via kie.ai
  (`tools/generate_hook_card_music.py`); the Music column (col F) picks a track
  by name, blank = random.
- Duration: 8s total, split evenly across scenes (Ken Burns each). Single
  scene = one 8s push.
- Fail-soft like `motion_ads`: never raise, write partial results.

## Chosen approach

Mirror `motion_ads` (article -> language -> copy -> image -> assemble ->
persist) and `4images` (multi-image row), reusing existing infra:

- `card_renderer.py` -> extend with `render_hook_overlay_bytes()` returning a
  transparent RGBA PNG (lower-third rounded box + wrapped white bold text).
- Rendi: add a Ken Burns (supersampled zoompan) still->clip command and a
  "set looped/trimmed music as the audio track" command (the existing
  `_MUSIC_MIX_TEMPLATE` assumes the video already has audio -> unusable on a
  silent slideshow). Reuse concat + PNG-overlay commands.
- New `row_processor_hook_card.py`, `hook_card_copy.py` (one-line hook + N
  scene descriptions when generating).
- Wiring seams (mirror motion_ads exactly): `models/row.py` (HookCardRow),
  `routes/jobs.py` (Pydantic in-model + coercion + tab branch), `queue.py`
  (serialize union), `runner.py` (dispatch), `apps_script/Code.gs` (TAB const,
  column parse, payload route).

Assembly (staged, each stage ffprobe-validated before the next):
1. Resolve N + gather/generate N background images.
2. Render one transparent hook-overlay PNG.
3. Per image, in parallel: Ken Burns -> silent normalized clip (identical
   size/SAR/fps so concat is safe).
4. Concat clips -> one silent slideshow.
5. One final call: overlay hook PNG + set looped/trimmed music.
6. Persist.

## Engineering guardrails (from the council pass)

- ffprobe-validate every render (duration within tolerance, non-black sample,
  audio stream present on the final) before persisting. Fail-soft without
  validation is fail-blind on a live ad account.
- Force-normalize (scale, pad, setsar, fps, pix_fmt) every clip before concat;
  manual and AI images differ in dimensions/SAR.
- Ken Burns = supersample (~3x) then linear `on`-based zoom, downscale -> no
  integer-rounding jitter.
- Music must be platform-cleared (Meta Sound Collection / TikTok Commercial
  Music Library / YouTube Audio Library), logged per row. "CC0" alone is not
  Content-ID-proof for paid ads.
- Reuse the existing no-real-brands guard on AI images; cap image-gen per row
  so a fail-soft retry cannot multiply cost.

## Alternatives rejected

- Single mega filter_complex (zoompan-per-input + concat + overlay + music in
  one Rendi call): fewer round-trips, but fails all-or-nothing with no probe
  surface, and Rendi has failed silently on complex graphs. Staged assembly is
  independently verifiable at each checkpoint. Rejected.
- Per-clip AI animation (Seedance) instead of Ken Burns: nicer motion but a
  per-clip AI cost x N scenes x every row. Rejected on cost (operator chose
  Ken Burns).
- Real stock footage (Pexels/Pixabay) as the default background: net-new
  adapter; operator chose manual-or-AI. Deferred (could be a later source).

## Security / safety

- Music licensing: platform-cleared source only; keep per-track provenance and
  log track->row so a claim's blast radius is traceable.
- Brand safety: reuse `no-real-brands-in-generated-images` guard + negative
  prompting on AI scenes; no real logos/trademarks in generated imagery.
- Cost control: per-row image cap; fail-soft must not retry image-gen
  unbounded.
- No secrets in code; manual image URLs pass through the existing url_guard.

## Open questions

- Music library: RESOLVED — a Suno-generated instrumental pool (9 named styles
  x 2 variations) via kie.ai, picked by name in col F (blank = random). Suno
  training-data litigation is a residual risk noted in the folder README.
- Manual cell containing a video URL (mp4): v1 treats manual cells as images
  (Ken Burns). Auto-detecting a video URL and using it as the clip directly is
  a fast follow.
- Whether total length should scale with scene count (operator open to it;
  default is fixed 8s).

## De-risk first

Prove the two net-new ffmpeg graphs + the overlay locally (local ffmpeg, no
Rendi spend) into a real 8s 9:16 sample before wiring the tab. POC:
`scratchpad/hook_card_poc.py`.


## v2 (2026-07-13): per-cell media, AI video, voiceover, naming

Column layout (v2): A Country · B Vertical · C Article · D Num of Images ·
E Text · F Voiceover · G Music · H-L Manual Media 1-5 · M Change Size ·
N Open Comments · O Ready Video.

- **Per-cell media** — each Manual Media cell (H-L) independently: image URL
  (Ken Burns), video URL (raw clip), `AI` (AI image), `AI Video` (Seedance).
  All blank -> `Num of Images` AI images. `_classify_media` decides per cell.
- **AI video** — `AI Video` cells generate a still (nano-banana) then animate it
  with Seedance (a per-scene paid cost; the operator opts in per cell).
- **Voiceover (col F)** — Yes -> a script from the article (reuses
  `classify_open_comments` + `generate_script` + TTS like the other tabs). The
  narration drives the video length; music is **ducked** under it (VO 100% /
  music ~30%). New Rendi commands: `set_vo` (VO-only) and `mix_vo_music`.
- **Mixed-fps concat** — `render_cartoon_concat_command` gained an optional
  `fps` param so Ken Burns / pasted-video / Seedance clips (any fps) join
  cleanly. hook_card passes `fps=30`; every other tab is unchanged.
- **Music `None`** — `select_track("None")` returns silent.
- **Output name** — `Country-Vertical-<langISO>-Music-RowNumber`
  (e.g. `SE-ShippingContainerHomes-sv-Piano1-5`). Language from detection
  (from the article, or the Text cell for fully-manual rows).
- **Write-back** — Ready Video is now col O (`sheets._HookCardCols`).

New ffmpeg graphs verified with local ffmpeg (VO mix drives length via
`amix duration=first`; fps-normalized concat merges 30fps + 24fps sources).
