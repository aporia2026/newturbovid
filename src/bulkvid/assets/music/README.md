# Hook Card background music

Drop background-music tracks for the `hook_card` tab in this folder. The row
processor rotates through them deterministically by row number
(`hook_card_music.py`), so a batch varies its music instead of repeating one
song. If this folder is empty, hook_card videos render **silent** (still valid)
and a warning is logged.

## Licensing — read before adding anything

These videos run on **paid ad accounts**. "Royalty-free" is NOT enough:
Meta, TikTok, and YouTube run their own audio fingerprinting (Content-ID), and
a third party can claim even a genuinely free track — a claim then hits every
ad using that track at once. Only add audio from a **platform-cleared** source:

- **Meta Sound Collection** — free, cleared for use in ads on Meta platforms.
  https://business.facebook.com/creativehub/sound
- **TikTok Commercial Music Library** — cleared for commercial/brand use on
  TikTok. https://www.tiktok.com/business/en/commercial-music-library
- **YouTube Audio Library** — filter to "No attribution required"; cleared for
  monetized/commercial use. https://www.youtube.com/audiolibrary

Pick the library that matches where the ads run. Keep a record of each track's
source and license (a `PROVENANCE.md` next to the files is fine) so a takedown
can be traced and swapped fast.

## Requirements

- Format: `.mp3` (preferred), `.m4a`, `.aac`, `.wav`, `.ogg`, or `.opus`.
- Length: **at least 15 seconds** — the assembler trims the track to the video
  length with `-shortest`; a track shorter than the video would cut the video
  off early.
- Keep files reasonably small (a few MB each) — they are uploaded per row.
