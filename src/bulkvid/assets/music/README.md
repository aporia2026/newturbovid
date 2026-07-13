# Hook Card background music

Instrumental background beds for the `hook_card` tab. The row processor plays
one under each slideshow: the sheet's **Music** column (col F) picks a track by
name, or a blank cell plays a random one.

## How these are made

Generated with Suno via the kie.ai API — `tools/generate_hook_card_music.py`.
Run it once to (re)populate this folder (it is resumable — existing names are
skipped):

    python tools/generate_hook_card_music.py

Every track is instrumental (no vocals). Files are named `<name>_<n>.mp3` where
`<name>` is the operator-facing pick label and `<n>` is the variation
(e.g. `uplifting_1.mp3`, `uplifting_2.mp3`). The pick names MUST stay in sync
across three places:

- `STYLES` in `tools/generate_hook_card_music.py`
- `HOOK_CARD_MUSIC_NAMES` in `apps_script/Code.gs` (the sheet dropdown)
- these file names

Current names: **Uplifting, Cinematic, Piano, Lofi, Acoustic, Energetic,
Ambient, Electronic, Indie**. See `PROVENANCE.md` for the exact files.

## Licensing

These are Suno-generated instrumentals under a paid/API tier that grants
commercial use, and being uniquely generated they avoid the platform Content-ID
claims that flag reused "royalty-free" audio. There is an open industry
question around Suno's training data — keep `PROVENANCE.md` for the record. To
swap in platform-cleared audio instead (Meta Sound Collection / TikTok
Commercial Music Library / YouTube Audio Library), just drop `.mp3`s here using
the same `<name>_<n>.mp3` convention.

## Requirements

- Format `.mp3` (also `.m4a/.aac/.wav/.ogg/.opus`); tracked via Git LFS.
- Length >= 15s — the assembler trims to the video length with `-shortest`.
- If this folder is empty, videos render silent (still valid) and a warning logs.
