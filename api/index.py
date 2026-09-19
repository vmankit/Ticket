"""Vercel entry point.

Vercel serves this file as a serverless function and looks for a WSGI callable
named `app`, so this only has to put the repository root on the import path and
re-export the Flask app that `app.py` already builds. Everything else - routes,
parsing, PDF generation - is unchanged, and Render keeps using the Dockerfile
and gunicorn instead of this file.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402  (the path has to be set before the import)

__all__ = ["app"]
