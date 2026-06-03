"""Local-dev Cloudflare quick-tunnel manager (test rig only).

Exposes the local backend to Google's Apps Script during local testing.
Spawns a DETACHED ``cloudflared tunnel --url http://127.0.0.1:<port>`` (so it
survives web-app restarts), captures the public ``*.trycloudflare.com`` URL,
and persists it to ``<data_dir>/tunnel.json`` so the admin panel can show it
and regenerate it on demand.

This is purely a local crutch — in production the host has its own public URL
and there is no tunnel. Guarded by ``BULKVID_MANAGE_TUNNEL`` in the admin routes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from bulkvid.logging import get_logger

_log = get_logger("tunnel")

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_WINGET = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    if _WINGET.exists():
        for exe in _WINGET.rglob("cloudflared.exe"):
            return str(exe)
    return None


class TunnelManager:
    """Manage a detached cloudflared quick tunnel + its URL state file."""

    def __init__(self, port: int, data_dir: Path) -> None:
        self._port = port
        self._dir = Path(data_dir)
        self._state = self._dir / "tunnel.json"
        self._logfile = self._dir / "cloudflared.log"

    def available(self) -> bool:
        return find_cloudflared() is not None

    def current_url(self) -> str | None:
        try:
            url = json.loads(self._state.read_text(encoding="utf-8")).get("url")
        except Exception:
            return None
        return url if isinstance(url, str) else None

    def set_url(self, url: str) -> None:
        """Record an externally-started tunnel URL (bootstrap)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        self._state.write_text(json.dumps({"url": url}), encoding="utf-8")

    # ── Regenerate (blocking work runs off the event loop) ──────────────────

    def _spawn_and_capture(self) -> str:
        exe = find_cloudflared()
        if not exe:
            raise RuntimeError("cloudflared not found on this machine")
        # Kill any existing tunnel so there's exactly one.
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/F", "/IM", "cloudflared.exe"],
                capture_output=True, check=False,
            )

        self._dir.mkdir(parents=True, exist_ok=True)
        logf = open(self._logfile, "w", encoding="utf-8")  # noqa: SIM115 (child owns it)
        flags = 0
        if sys.platform == "win32":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP -> survives web-app restart.
            flags = 0x00000008 | 0x00000200
        subprocess.Popen(
            [exe, "tunnel", "--url", f"http://127.0.0.1:{self._port}"],
            stdout=logf, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True,
        )

        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                m = _URL_RE.search(self._logfile.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                m = None
            if m:
                url = m.group(0)
                self._state.write_text(json.dumps({"url": url}), encoding="utf-8")
                _log.info("tunnel_regenerated", url=url)
                return url
            time.sleep(1)
        raise RuntimeError("cloudflared did not report a URL within 45s")

    async def regenerate(self) -> str:
        return await asyncio.to_thread(self._spawn_and_capture)
