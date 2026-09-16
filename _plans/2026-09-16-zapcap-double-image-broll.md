# Fix: ZapCap "double image" on 1-click-image-vid (Auto B-Roll)

Date: 2026-09-16
Branch: `oci-zapcap-aspect` (worktree `../turbovid-new-oci-zapcap-aspect`)

## Symptom

On the `1-click-image-vid` tab, enabling the **ZapCap** column produced a video
with a "double image" — an unrelated scene spliced into part of the frame on top
of the intended story still. With ZapCap **off**, the same row rendered correctly
(one story scene filling the frame at a time). Confirmed by the operator.

## Root cause (verified, not guessed)

ZapCap has an **Auto B-Roll** feature controlled by
`transcribeSettings.broll.brollPercent`. Per the official docs
(https://platform.zapcap.ai/docs/configuration/) it **defaults to 50** and only
fires on videos longer than ~8-10 seconds, splicing stock/AI b-roll footage into
roughly that percentage of the timeline.

Our `ZapCapClient.create_task` never sent `transcribeSettings`, so ZapCap applied
its default (b-roll on, 50%). Every ZapCap-using tab uses the same adapter, but:

- Motion tabs (cartoon, simple_motion, avatar, ...) target ~8s or feed per-clip
  motion video — under the b-roll trigger, so it never fired.
- `one_click_image_vid` sizes its video to the voiceover with an 8s floor
  (`OCI_MIN_VIDEO_SECONDS`); real rows land at 10-13s+, crossing the threshold, so
  ZapCap injected b-roll = the "double image."

Ruled out by evidence: the assembled video is correct with ZapCap off (so it is
not the 2x2 collage split, not the aspect ratio, not our concat). The doubling is
introduced entirely by the ZapCap service.

## Fix

Adapter-level, single source of truth: `create_task` (and the `caption_video`
wrapper) now send `transcribeSettings.broll.brollPercent`, defaulting to **0**
(`ZAPCAP_DEFAULT_BROLL_PERCENT`). Value is clamped to 0-100. An optional
`broll_percent` parameter allows opting back in later if ever wanted.

Rationale for disabling globally, not just on this tab: no tab intends ZapCap to
insert third-party footage; every tab produces its own visuals. Auto b-roll on
brand / finance / health content is also a content-safety liability. Disabling it
by default fixes the reported bug and the same latent bug on any tab whose video
crosses the 8-10s trigger.

### Alternatives considered

1. **Disable b-roll only in the one_click processor call.** Rejected — leaves the
   latent bug on every other tab and duplicates the choice at each call site
   (violates SSOT).
2. **Force the video to a shorter length so it stays under the trigger.**
   Rejected — degrades the product (VO gets cut) to work around a setting, and the
   trigger threshold is undocumented/unstable.
3. **Switch to a different ZapCap template with no b-roll.** Rejected — b-roll is a
   task setting, not a template property; the shared template is fine once b-roll
   is off, and swapping templates changes caption styling for all tabs.

## Observability

`zapcap_task_create` now logs `broll_percent` so the setting is visible per task.

## Testing

`tests/unit/test_zapcap.py`:
- `test_create_task_disables_broll_by_default` — regression; fails on the pre-fix
  adapter (no `transcribeSettings` key), passes now.
- `test_create_task_broll_percent_override_and_clamp` — explicit value honoured,
  out-of-range clamped to 0-100.
- `test_caption_video_disables_broll_by_default` — the end-to-end wrapper every
  row processor uses defaults to b-roll off.

Full `tests/unit` suite run green against the worktree source.

Note: `bulkvid` is installed editable against the MAIN checkout, so tests in this
worktree must run with `PYTHONPATH=<worktree>/src` to exercise the branch code.

## Deploy

Standard flow: PR `oci-zapcap-aspect` -> `main`. After merge, production deploy is
`git push hf main:main` (and the `hf2` snapshot for bulkvid2), per
`turbovid_deploy_flow`. No Code.gs / Apps Script change — this is backend only.
Rollback: revert the merge commit and re-push.
