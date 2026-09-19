"""Vercel entry point.

Vercel serves this file as a serverless function and looks for a WSGI callable
named `app`, so this puts the repository root on the import path and re-exports
the Flask app that `app.py` already builds. Render is unaffected: it builds
from the Dockerfile and serves `app:app` with gunicorn, never this file.

The rewrite in vercel.json sends every URL here, and Vercel routes backend
frameworks using the rewritten destination path, so the function is invoked at
`/api/index` rather than the URL the visitor asked for. Flask would match no
route and answer 404 for the whole site. The middleware below restores the
requested path before Flask sees the request.
"""

import json
import os
import sys
from urllib.parse import parse_qsl, unquote, urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app  # noqa: E402  (path set before the import)

# Name the rewrite uses to carry the visitor's path. Anything a visitor sends
# under this name is overwritten by the rewrite, so it cannot be spoofed.
PATH_PARAM = "__vpath"

# Headers Vercel has been observed to carry the original request path in, tried
# in order when the query parameter does not arrive - a rewrite's destination
# query string is not guaranteed to reach the function.
PATH_HEADERS = (
    "HTTP_X_VERCEL_ORIGINAL_PATH",
    "HTTP_X_VERCEL_REWRITE_PATH",
    "HTTP_X_FORWARDED_URI",
    "HTTP_X_ORIGINAL_URL",
)

# Where the function itself lives. Only a request arriving at exactly this path
# has lost its original URL and needs one recovered.
FUNCTION_PATH = "/api/index"


class RestoreOriginalPath:
    """Put the requested path back into the WSGI environ.

    A no-op when the request already carries a real path, so running locally
    and Render's gunicorn path are untouched, and it stays correct if Vercel
    ever forwards the original path itself.
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        original, query = self._carried_path(environ)

        if original is None and environ.get("PATH_INFO", "") in (FUNCTION_PATH,
                                                                 FUNCTION_PATH + ".py"):
            original, query = self._header_path(environ)

        if original:
            if not original.startswith("/"):
                original = "/" + original
            environ["PATH_INFO"] = original
            if query is not None:
                environ["QUERY_STRING"] = query
        elif environ.get("PATH_INFO", "") in (FUNCTION_PATH, FUNCTION_PATH + ".py"):
            # Nothing carried the visitor's path, so every URL would 404 with
            # no clue as to why. Say what did arrive instead: this cannot fire
            # once a path is recovered, and it names only routing keys.
            return self._explain(environ, start_response)

        return self.wsgi_app(environ, start_response)

    def _explain(self, environ, start_response):
        seen = {name[5:].lower().replace("_", "-"): environ[name]
                for name in PATH_HEADERS if environ.get(name)}
        body = json.dumps({
            "error": "Could not recover the requested path",
            "message": ("The rewrite did not carry it and no known header "
                        "holds it, so Flask has no route to match."),
            "path_info": environ.get("PATH_INFO", ""),
            "query_string": environ.get("QUERY_STRING", ""),
            "path_headers_present": seen,
        }, indent=2).encode()
        start_response("500 Internal Server Error", [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ])
        return [body]

    def _carried_path(self, environ):
        """The path from the rewrite's query parameter, if it arrived."""
        pairs = parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        found = None
        remaining = []
        for key, value in pairs:
            if key == PATH_PARAM and found is None:
                found = value
            else:
                remaining.append((key, value))
        if found is None:
            return None, None
        # The app's own query string is whatever is left once the carried path
        # is removed, so /api/search-airports?q=del still sees q.
        return found, urlencode(remaining)

    def _header_path(self, environ):
        """The path from whichever header carries it, query string included."""
        for header in PATH_HEADERS:
            raw = environ.get(header, "").strip()
            if not raw:
                continue
            path, sep, query = raw.partition("?")
            path = unquote(path)
            if path and path != FUNCTION_PATH:
                return path, (query if sep else None)
        return None, None


flask_app.wsgi_app = RestoreOriginalPath(flask_app.wsgi_app)

app = flask_app

__all__ = ["app"]
