"""Pre-restart autopsy — capture what every thread was doing before we exit.

Why this exists (Plan
``_plans/2026-08-12-worker-liveness-net-heartbeat-forensics.md``):

Every watchdog in this codebase recovers by killing the process — which is also
the only process that knows why it wedged. HuggingFace Spaces retains container
logs only since the last start, so each of the six incidents to date destroyed
its own evidence, and every fix since has been aimed at an UNCONFIRMED cause.
That is why each one patched the previous incident's shape while the next wedge
found a new seam.

This module closes that loop. Immediately before ``os._exit``, a watchdog calls
``capture_thread_stacks()`` and gets a formatted dump of every live thread —
including the worker's MainThread, i.e. the asyncio event loop that a wedge most
likely has blocked, and any ``bulkvid-db`` pool thread parked forever inside an
uncancellable libsql call. The caller persists it (the DB survives the restart)
and ``dump_to_stderr()`` also puts it in the live log for the operator watching
right now.

Deliberately dependency-free and side-effect-free: ``sys._current_frames()`` is
a plain snapshot of interpreter state that cannot block on a lock, a socket, or
the GIL-holding thread we are trying to diagnose. Anything heavier (py-spy,
signal-based dumps) could hang inside the very wedge it is documenting.
"""

from __future__ import annotations

import faulthandler
import sys
import threading
import traceback
from contextlib import suppress

# Cap per thread so one deep recursion can't crowd the interesting frames out of
# the stored dump. Generous: real stacks here run ~20-40 frames.
_MAX_FRAMES_PER_THREAD = 60
# Overall cap on the stored text. Comfortably fits ~20 threads of full stacks;
# guards the DB row (and the admin page) against a pathological dump.
_MAX_STACKS_CHARS = 60_000


def capture_thread_stacks() -> str:
    """Formatted stack trace of every live thread, newest frame last.

    Never raises — this runs on the failure path, where a second failure would
    cost us the evidence entirely. Any error is returned AS the text so the
    forensics row records that the capture itself failed rather than silently
    storing nothing."""
    try:
        names = {t.ident: t.name for t in threading.enumerate()}
        frames = sys._current_frames()
        chunks: list[str] = []
        for ident, frame in frames.items():
            name = names.get(ident, "unknown")
            stack = traceback.format_stack(frame)[-_MAX_FRAMES_PER_THREAD:]
            chunks.append(
                f"--- thread {name} (id={ident}) ---\n{''.join(stack)}"
            )
        text = "\n".join(chunks) if chunks else "no frames captured"
    except Exception as e:    # noqa: BLE001 — the failure path must not fail
        return f"stack capture failed: {type(e).__name__}: {e}"
    if len(text) > _MAX_STACKS_CHARS:
        return text[:_MAX_STACKS_CHARS] + "\n... [truncated]"
    return text


def dump_to_stderr(reason: str) -> None:
    """Best-effort ``faulthandler`` dump of all threads to stderr.

    Complements the DB row: stderr reaches the HF log tab immediately, which is
    what an operator is staring at during an incident, and it is the ONLY
    forensics available to a watchdog with no DB credentials (``db_watchdog``).
    Wrapped in ``suppress`` because a closed/redirected stderr must not stop the
    restart that follows."""
    with suppress(Exception):
        print(f"[wedge forensics] {reason}", file=sys.stderr, flush=True)
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
