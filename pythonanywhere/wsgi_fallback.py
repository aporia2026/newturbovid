"""WSGI entrypoint for PythonAnywhere (Manual-configuration web app).

PythonAnywhere's native ASGI support is still beta, so we run the FastAPI
(ASGI) app under WSGI via ``a2wsgi.ASGIMiddleware``. Our web endpoints are
light (enqueue + status + admin) and we use no streaming/WebSockets, so the
WSGI surface is a clean, stable fit.

``init_wsgi()`` builds ``app.state`` (queue, verifier, settings store) because
FastAPI's ASGI ``lifespan`` does NOT run under WSGI.

On PythonAnywhere, set the Web tab's WSGI configuration file to:

    import os
    os.chdir("/home/USERNAME/bulkvid")          # so .env and ./data resolve
    from a2wsgi import ASGIMiddleware
    from bulkvid.main import init_wsgi
    application = ASGIMiddleware(init_wsgi())

Plan: _plans/2026-06-02-aporia-bulk-video-tool.md §5 (deploy).
"""

from __future__ import annotations

from a2wsgi import ASGIMiddleware

from bulkvid.main import init_wsgi

# WSGI servers look for a module-level ``application``.
application = ASGIMiddleware(init_wsgi())
