"""Upload the two card-template preview PNGs to cloud storage.

One-shot script — run whenever a template preview asset changes:

    python tools/upload_template_previews.py

Reads source PNGs from ``apps_script/template_previews/template_{1,2}.png``,
uploads them to the configured storage backend (GCS primary, S3 fallback per
``StorageClient.build_client_from_settings``) under
``bulkvid/templates/template_{1,2}.png``, and prints the public URLs so they
can be pasted into the ``card_preview_url_template_*`` settings.

Plan: ``_plans/2026-06-08-simple-x4-template-cards.md`` §D.7.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from bulkvid.adapters.storage import build_client_from_settings    # noqa: E402
from bulkvid.logging import get_logger    # noqa: E402

_log = get_logger("upload_template_previews")


# Source files in the repo. The Apps Script side stores the operator-supplied
# mockups here so they're under version control; this tool ships them to
# storage on demand.
SOURCES: tuple[tuple[str, str], ...] = (
    ("template_1.png", "bulkvid/templates/template_1.png"),
    ("template_2.png", "bulkvid/templates/template_2.png"),
    ("template_3.png", "bulkvid/templates/template_3.png"),
)

SOURCE_DIR = REPO_ROOT / "apps_script" / "template_previews"


async def _upload_one(client, local_filename: str, remote_key: str) -> str:
    """Upload one PNG and return the public URL."""
    src_path = SOURCE_DIR / local_filename
    if not src_path.is_file():
        raise FileNotFoundError(f"missing source PNG: {src_path}")
    data = src_path.read_bytes()
    _log.info(
        "preview upload start",
        local=str(src_path.relative_to(REPO_ROOT)),
        remote_key=remote_key,
        bytes=len(data),
    )
    result = await client.upload_bytes(
        data=data,
        key=remote_key,
        content_type="image/png",
    )
    _log.info(
        "preview upload ok",
        backend=result.backend,
        url=result.url,
        bytes_written=result.bytes_written,
    )
    return result.url


async def _main() -> int:
    try:
        client = build_client_from_settings()
    except ValueError as e:
        # No storage configured — print actionable hint and exit non-zero so
        # CI / wrappers can detect the misconfiguration.
        print(f"ERROR: storage not configured: {e}", file=sys.stderr)
        return 2

    print("Uploading template previews…")
    urls: list[tuple[str, str]] = []
    for local_filename, remote_key in SOURCES:
        try:
            url = await _upload_one(client, local_filename, remote_key)
            urls.append((local_filename, url))
        except FileNotFoundError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 3
        except Exception as e:
            print(f"ERROR uploading {local_filename}: {e}", file=sys.stderr)
            return 4

    # Print the URLs at the end in a stable shape so they're easy to paste
    # into the settings page (or copy into a script).
    print("\n=== Public URLs (paste into settings) ===")
    for local_filename, url in urls:
        setting_key = (
            "card_preview_url_template_1"
            if local_filename == "template_1.png"
            else "card_preview_url_template_2"
        )
        print(f"{setting_key} = {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
