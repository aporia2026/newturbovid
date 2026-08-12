"""Pre-restart autopsy capture (Plan 2026-08-12).

This module runs on the failure path, microseconds before ``os._exit``, so the
bar is not just "produces a useful dump" but "cannot itself fail". Both are
covered here:
  * ``capture_thread_stacks`` includes the calling thread and any live sibling.
  * it returns an explanatory string rather than raising when the interpreter
    introspection it relies on breaks.
  * it truncates a pathological dump instead of returning unbounded text.
  * ``dump_to_stderr`` writes the reason and never raises, even on a dead
    stream.
"""

from __future__ import annotations

import threading

from bulkvid.orchestrator import forensics


def test_capture_includes_current_thread() -> None:
    text = forensics.capture_thread_stacks()
    assert "--- thread " in text
    # The frame of this very test must appear — that is the whole point: the
    # dump has to show what the wedged thread was actually executing.
    assert "test_capture_includes_current_thread" in text


def test_capture_includes_a_live_sibling_thread() -> None:
    """A wedge lives in *another* thread (the event loop, a parked libsql pool
    thread), so the dump must span all of them, not just the watchdog's."""
    started = threading.Event()
    release = threading.Event()

    def _park() -> None:
        started.set()
        release.wait()

    t = threading.Thread(target=_park, name="bulkvid-test-parked", daemon=True)
    t.start()
    try:
        started.wait(timeout=5)
        text = forensics.capture_thread_stacks()
        assert "bulkvid-test-parked" in text
    finally:
        release.set()
        t.join(timeout=5)


def test_capture_returns_reason_instead_of_raising(monkeypatch) -> None:
    """A second failure on the failure path would cost us the evidence
    entirely, so a broken capture reports itself in-band."""
    def _boom():
        raise RuntimeError("frames unavailable")

    monkeypatch.setattr(forensics.sys, "_current_frames", _boom)
    text = forensics.capture_thread_stacks()
    assert "stack capture failed" in text
    assert "frames unavailable" in text


def test_capture_truncates_a_pathological_dump(monkeypatch) -> None:
    monkeypatch.setattr(forensics, "_MAX_STACKS_CHARS", 200)
    text = forensics.capture_thread_stacks()
    assert text.endswith("... [truncated]")
    assert len(text) <= 200 + len("\n... [truncated]")


def test_dump_to_stderr_writes_reason(capsys) -> None:
    forensics.dump_to_stderr("worker_heartbeat_stale")
    err = capsys.readouterr().err
    assert "worker_heartbeat_stale" in err


def test_dump_to_stderr_never_raises(monkeypatch) -> None:
    """A closed or redirected stderr must not stop the restart that follows."""
    def _boom(*_a, **_k):
        raise OSError("stderr is closed")

    monkeypatch.setattr(forensics.faulthandler, "dump_traceback", _boom)
    forensics.dump_to_stderr("stuck_queue_idle")    # no exception = pass
