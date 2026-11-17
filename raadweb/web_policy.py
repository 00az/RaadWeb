"""The response headers, caching and content types the landing site is served with.

One source of truth: the CloudFront stack sets these values, the smoke test checks for them on
the deployed site, and the browser scan's local server sends them so the pages are tested under
the same Content Security Policy they are served with.
"""

from __future__ import annotations

from types import MappingProxyType

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
HSTS_MAX_AGE_SECONDS = 31_536_000  # one year
STRICT_TRANSPORT_SECURITY = f"max-age={HSTS_MAX_AGE_SECONDS}"
REFERRER_POLICY = "no-referrer"
CONTENT_TYPE_OPTIONS = "nosniff"
FRAME_OPTIONS = "DENY"

SECURITY_HEADERS = MappingProxyType(
    {
        "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        "Strict-Transport-Security": STRICT_TRANSPORT_SECURITY,
        "X-Content-Type-Options": CONTENT_TYPE_OPTIONS,
        "Referrer-Policy": REFERRER_POLICY,
        "X-Frame-Options": FRAME_OPTIONS,
    }
)

# Pages: browsers always revalidate; CloudFront keeps a copy for at most a minute, and every
# deploy invalidates both pages.
HTML_CACHE_CONTROL = "public, max-age=0, s-maxage=60, must-revalidate"
# Hashed assets never change under the same name, so they are cached for a year.
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
# Files that keep a fixed name (favicon.ico, the font licence) are cached for a day.
UNHASHED_CACHE_CONTROL = "public, max-age=86400"
# Error pages are cached briefly at the edge, so a page that appears on the next deploy shows.
ERROR_CACHE_SECONDS = 30

# File types the build gives content-hashed names. They may only live under `assets/`, so nothing
# with a fixed name is ever cached for a year.
HASHED_SUFFIXES = frozenset({".css", ".woff2", ".webp", ".png", ".svg"})

# Set explicitly on upload rather than guessed, so `nosniff` never meets a wrong type.
CONTENT_TYPES = MappingProxyType(
    {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".woff2": "font/woff2",
        ".webp": "image/webp",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".txt": "text/plain; charset=utf-8",
    }
)
