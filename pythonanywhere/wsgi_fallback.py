"""WSGI entrypoint for PythonAnywhere (Manual-configuration web app).

PythonAnywhere's native ASGI support is still beta, so we run the FastAPI
(ASGI) app under WSGI via ``a2wsgi.ASGIMiddleware``. Our web endpoints are
light (enqueue + status + admin) and we use no streaming/WebSockets, so the
WSGI surface is a clean, stable fit.

How to use on PythonAnywhere (Web tab -> WSGI configuration file): replace the
file's contents with the snippet below, adjusting USERNAME. (We keep the logic
here too, but PA loads its own ``/var/www/..._wsgi.py``.)

    import os
    os.chdir("/home/USERNAME/bulkvid")          # so .env and ./data resolve
    from a2wsgi import ASGIMiddleware
    from bulkvid.main import app
    application = ASGIMiddleware(app)

Plan: _plans/2026-06-02-aporia-bulk-video-tool.md §5 (deploy).
"""

from __future__ import annotations

from a2wsgi import ASGIMiddleware

from bulkvid.main import app

# WSGI servers look for a module-level ``application``.
application = ASGIMiddleware(app)
