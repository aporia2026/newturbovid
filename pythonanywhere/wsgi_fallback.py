"""WSGI shim — only used if ASGI beta on PA stops working.

If/when PythonAnywhere yanks ASGI support or changes the deploy model, point
the PA Web tab WSGI file at this module. It runs the FastAPI app through
``a2wsgi.ASGIMiddleware``, sacrificing streaming endpoints and WebSockets
(we don't use either) for a stable WSGI surface.

Phase 0: stub. Not wired by default. Enable only if `pa website` ASGI fails.

Plan: _plans/2026-06-02-aporia-bulk-video-tool.md §5 (migration triggers).
"""

# Uncomment if/when needed (also add `a2wsgi>=1.10` to pyproject):
#
# from a2wsgi import ASGIMiddleware
# from bulkvid.main import app
#
# application = ASGIMiddleware(app)
