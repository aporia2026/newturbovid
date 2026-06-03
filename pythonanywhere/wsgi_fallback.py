"""WSGI entrypoint for PythonAnywhere (Manual-configuration web app).

PythonAnywhere's native ASGI support is still beta, so we run the FastAPI
(ASGI) app under WSGI via ``a2wsgi.ASGIMiddleware``. Our web endpoints are
light (enqueue + status + admin) and we use no streaming/WebSockets, so the
WSGI surface is a clean, stable fit.

CRITICAL — build lazily, per worker, on the first request. PythonAnywhere's
uWSGI *pre-forks* its workers: anything created at import time (in the master)
is forked into the workers as a dead copy. a2wsgi starts a background
event-loop thread, and ``init_wsgi()`` opens a SQLite connection — neither
survives the fork, so every request blocks forever and uWSGI HARAKIRI-kills the
worker. Constructing them inside ``application()`` defers creation until after
the fork, in the worker that will actually use them.

``init_wsgi()`` builds ``app.state`` (queue, verifier, settings store) because
FastAPI's ASGI ``lifespan`` does NOT run under WSGI.

On PythonAnywhere, set the Web tab's WSGI configuration file to:

    import os
    os.chdir("/home/USERNAME/bulkvid")          # so .env and ./data resolve

    _wrapped = None

    def application(environ, start_response):
        global _wrapped
        if _wrapped is None:
            from a2wsgi import ASGIMiddleware
            from bulkvid.main import init_wsgi
            _wrapped = ASGIMiddleware(init_wsgi())
        return _wrapped(environ, start_response)

Plan: _plans/2026-06-02-aporia-bulk-video-tool.md §5 (deploy).
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

# Built on the first request, per worker, AFTER uWSGI forks (see module docstring).
_wrapped: Callable[..., Iterable[bytes]] | None = None


def application(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
    """WSGI callable. Lazily wraps the ASGI app on first use so a2wsgi's loop
    thread and the SQLite connection are created in the (post-fork) worker."""
    global _wrapped
    if _wrapped is None:
        from a2wsgi import ASGIMiddleware

        from bulkvid.main import init_wsgi

        _wrapped = ASGIMiddleware(init_wsgi())
    return _wrapped(environ, start_response)
