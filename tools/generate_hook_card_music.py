"""Generate the hook_card background-music pool via Suno (kie.ai), one time.

The hook_card tab plays a bundled instrumental bed under the slideshow and
rotates through whatever tracks live in ``src/bulkvid/assets/music/``. This
script populates that folder: it asks Suno (through kie.ai) for a handful of
INSTRUMENTAL beds across a spread of moods, polls each to completion, downloads
the MP3s, and writes them into the folder (plus a PROVENANCE.md).

Run once (it is resumable — a style whose files already exist is skipped):

    python tools/generate_hook_card_music.py            # full pool
    python tools/generate_hook_card_music.py --limit 1  # smoke-test one style
    python tools/generate_hook_card_music.py --model V5 # override the model

Cost: a few kie credits per style (each generation returns 2 variations), so
the full pool is roughly $1-2 one time. Uses the FIRST key in KIE_AI_KEYS.

Licensing note: these are Suno-generated instrumentals. See the folder README —
Suno's paid/API tiers grant commercial use and uniquely-generated tracks avoid
platform Content-ID claims, but keep this provenance for the record.

Endpoints (docs.kie.ai/suno-api): POST /api/v1/generate,
GET /api/v1/generate/record-info?taskId=...
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

# Make ``bulkvid`` importable when run as ``python tools/...`` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bulkvid.config import get_settings

MUSIC_DIR = Path(__file__).resolve().parent.parent / "src" / "bulkvid" / "assets" / "music"

# Terminal Suno states from the record-info endpoint.
_SUCCESS_STATES = {"SUCCESS"}
_FAIL_STATES = {
    "CREATE_TASK_FAILED",
    "GENERATE_AUDIO_FAILED",
    "CALLBACK_EXCEPTION",
    "SENSITIVE_WORD_ERROR",
}

# kie requires a callBackUrl; we poll instead, so a harmless placeholder is fine.
_CALLBACK_PLACEHOLDER = "https://example.com/suno-callback"


@dataclass(frozen=True)
class Style:
    slug: str        # file-name stem, e.g. "uplifting_corporate"
    title: str       # Suno track title (<=80 chars)
    style: str       # Suno style / genre-mood tags
    prompt: str      # short vibe description


# A spread of moods that suit faceless short-form ad backgrounds — broadly
# usable across markets and verticals. Instrumental, mid-energy, loopable.
# ``slug`` is the file-name stem AND the operator-facing pick name (1-2 words);
# it MUST match HOOK_CARD_MUSIC_NAMES in apps_script/Code.gs.
STYLES: list[Style] = [
    Style("uplifting", "Uplifting",
          "uplifting corporate, motivational, bright, clean pop production",
          "A bright, optimistic instrumental with light piano, soft claps and a steady motivational build."),
    Style("cinematic", "Cinematic",
          "cinematic, inspirational, soft strings, piano, gentle build",
          "A warm cinematic instrumental with soft strings and piano rising to a hopeful swell."),
    Style("piano", "Piano",
          "gentle solo piano, calm, reflective, minimal",
          "A calm, reflective solo piano instrumental, unhurried and warm."),
    Style("lofi", "Lofi",
          "lo-fi hip hop, chill, warm, mellow, relaxed beat",
          "A relaxed lo-fi instrumental with a soft mellow beat and warm keys."),
    Style("acoustic", "Acoustic",
          "warm acoustic guitar, light, friendly, folk pop",
          "A light, friendly acoustic-guitar instrumental with a gentle rhythm."),
    Style("energetic", "Energetic",
          "energetic pop, upbeat, driving, positive, modern",
          "An upbeat, driving pop instrumental with a positive, modern energy."),
    Style("ambient", "Ambient",
          "ambient, airy pads, soft, spacious, background",
          "A soft ambient instrumental of airy pads and gentle texture, spacious and calm."),
    Style("electronic", "Electronic",
          "modern electronic pop, clean, upbeat, glossy",
          "A clean, glossy electronic-pop instrumental with an upbeat, modern feel."),
    Style("indie", "Indie",
          "hopeful indie, light percussion, bright, positive",
          "A bright, hopeful indie instrumental with light percussion and a positive lift."),
]


async def _generate(client: httpx.AsyncClient, base: str, key: str, s: Style, model: str) -> str:
    """Submit one Suno generation; returns the taskId."""
    resp = await client.post(
        f"{base}/api/v1/generate",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "prompt": s.prompt,
            "customMode": True,
            "instrumental": True,
            "model": model,
            "style": s.style,
            "title": s.title,
            "negativeTags": "vocals, lyrics, spoken word, harsh, aggressive",
            "callBackUrl": _CALLBACK_PLACEHOLDER,
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 200:
        raise RuntimeError(f"generate failed for {s.slug}: {body}")
    task_id = (body.get("data") or {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"generate returned no taskId for {s.slug}: {body}")
    return task_id


async def _poll(
    client: httpx.AsyncClient, base: str, key: str, task_id: str,
    *, max_attempts: int = 60, delay: float = 6.0,
) -> list[str]:
    """Poll record-info until SUCCESS; return the audio URLs (usually 2)."""
    url = f"{base}/api/v1/generate/record-info"
    for attempt in range(max_attempts):
        resp = await client.get(
            url, headers={"Authorization": f"Bearer {key}"},
            params={"taskId": task_id}, timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        status = str(data.get("status") or "").upper()
        if status in _SUCCESS_STATES:
            suno = ((data.get("response") or {}).get("sunoData")) or []
            urls = [t.get("audioUrl") for t in suno if t.get("audioUrl")]
            if not urls:
                raise RuntimeError(f"SUCCESS but no audioUrl for task {task_id}: {data}")
            return urls
        if status in _FAIL_STATES:
            raise RuntimeError(f"task {task_id} failed: status={status} body={data}")
        print(f"    ... {status or 'PENDING'} ({attempt + 1}/{max_attempts})")
        await asyncio.sleep(delay)
    raise TimeoutError(f"task {task_id} did not finish in {max_attempts} polls")


async def _download(client: httpx.AsyncClient, url: str, dest: Path) -> int:
    resp = await client.get(url, timeout=180.0, follow_redirects=True)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return len(resp.content)


async def main() -> int:
    ap = argparse.ArgumentParser(description="Generate the hook_card music pool via Suno.")
    ap.add_argument("--limit", type=int, default=0, help="only the first N styles (0 = all)")
    ap.add_argument("--model", default="V4_5", help="Suno model (V4_5, V5, ...)")
    args = ap.parse_args()

    settings = get_settings()
    keys = settings.kie_key_list
    if not keys:
        print("ERROR: no KIE_AI_KEYS configured (.env)", file=sys.stderr)
        return 2
    key = keys[0]
    base = settings.KIE_BASE_URL.rstrip("/")

    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    styles = STYLES[: args.limit] if args.limit > 0 else STYLES

    made: list[str] = []
    async with httpx.AsyncClient() as client:
        for s in styles:
            existing = sorted(MUSIC_DIR.glob(f"{s.slug}_*.mp3"))
            if existing:
                print(f"[skip] {s.slug} — {len(existing)} file(s) already present")
                continue
            print(f"[gen ] {s.slug} — {s.style}")
            t0 = time.monotonic()
            task_id = await _generate(client, base, key, s, args.model)
            urls = await _poll(client, base, key, task_id)
            for i, u in enumerate(urls, start=1):
                dest = MUSIC_DIR / f"{s.slug}_{i}.mp3"
                n = await _download(client, u, dest)
                made.append(dest.name)
                print(f"    saved {dest.name} ({n // 1024} KB)")
            print(f"    done in {time.monotonic() - t0:.0f}s ({len(urls)} tracks)")

    if made:
        prov = MUSIC_DIR / "PROVENANCE.md"
        lines = [
            "# Provenance",
            "",
            "These background tracks were generated with Suno via the kie.ai API",
            "(`tools/generate_hook_card_music.py`). Instrumental, no vocals.",
            "Suno paid/API-tier commercial use; keep this note for the record.",
            "",
            "Tracks:",
        ]
        track_files = sorted(MUSIC_DIR.glob("*.mp3"), key=lambda p: p.name)
        lines += [f"- {p.name}" for p in track_files]
        prov.write_text("\n".join(lines) + "\n")
    print(f"\nGenerated {len(made)} track(s) into {MUSIC_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
