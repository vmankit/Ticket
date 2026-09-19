"""Vercel entry point.

Vercel serves this file as a serverless function and looks for a WSGI callable
named `app`, so this puts the repository root on the import path and re-exports
the Flask app that `app.py` already builds. Render is unaffected: it builds
from the Dockerfile and serves `app:app` with gunicorn, never this file.

The rewrite in vercel.json sends every URL here, and the function is invoked at
its own path - `/api/index` - rather than the one the visitor asked for, so
Flask would match no route and answer 404 for the whole site. The rewrite
therefore carries the original path in a query parameter and the middleware
below puts it back before Flask sees the request.
"""

import os
import sys
from urllib.parse import parse_qsl, urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app  # noqa: E402  (path set before the import)

# Name the rewrite uses to carry the visitor's path. Anything a visitor sends
# under this name is overwritten by the rewrite, so it cannot be spoofed.
PATH_PARAM = "__vpath"


class RestoreOriginalPath:
    """Put the requested path back into the WSGI environ.

    Harmless if Vercel ever starts forwarding the original path itself: the
    parameter then holds that same path. With the parameter absent - running
    locally, or under gunicorn - the environ is passed through untouched.
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        pairs = parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        original = None
        remaining = []
        for key, value in pairs:
            if key == PATH_PARAM and original is None:
                original = value
            else:
                remaining.append((key, value))

        if original:
            if not original.startswith("/"):
                original = "/" + original
            environ["PATH_INFO"] = original
            # The app's own query string is whatever is left once the carried
            # path is removed, so /api/search-airports?q=del still sees q.
            environ["QUERY_STRING"] = urlencode(remaining)

        return self.wsgi_app(environ, start_response)


flask_app.wsgi_app = RestoreOriginalPath(flask_app.wsgi_app)

app = flask_app

__all__ = ["app"]
