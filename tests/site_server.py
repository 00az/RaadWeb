"""A local static server for tests: serves a built site on 127.0.0.1 with chosen headers.

It stands in for CloudFront: `/en/` serves `en/index.html`, a missing path gets the site's
`404.html` with status 404 (as the distribution's error responses do), every response carries the
headers given (normally the production security headers), and a test can override any path's
status or body to simulate a broken deploy. Nothing leaves the machine.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from os import PathLike
from pathlib import Path
from typing import Any

from raadweb.web_policy import CONTENT_TYPES, SECURITY_HEADERS


@dataclass(frozen=True)
class Override:
    status: int
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"


class _Server(ThreadingHTTPServer):
    headers: Mapping[str, str]
    overrides: Mapping[str, Override]


class _Handler(SimpleHTTPRequestHandler):
    # Overrides the stdlib's per-instance attribute, so it is not a ClassVar.
    extensions_map = {  # noqa: RUF012
        **SimpleHTTPRequestHandler.extensions_map,
        **CONTENT_TYPES,
    }
    server: _Server

    def end_headers(self) -> None:
        for name, value in self.server.headers.items():
            self.send_header(name, value)
        super().end_headers()

    def do_GET(self) -> None:
        override = self.server.overrides.get(self.path)
        if override is None:
            super().do_GET()
            return
        self.send_response(override.status)
        self.send_header("Content-Type", override.content_type)
        self.send_header("Content-Length", str(len(override.body)))
        self.end_headers()
        self.wfile.write(override.body)

    def list_directory(self, path: str | PathLike[str]) -> None:
        """Like S3 behind CloudFront, never list a folder."""
        self.send_error(404, "Not Found")

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Answer a miss with the site's not-found page, as CloudFront's error responses do."""
        page = Path(self.directory) / "404.html"
        if code != 404 or not page.is_file():
            super().send_error(code, message, explain)
            return
        body = page.read_bytes()
        self.send_response(404)
        self.send_header("Content-Type", CONTENT_TYPES[".html"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep test output quiet."""


@contextmanager
def serve(
    root: Path,
    *,
    headers: Mapping[str, str] = SECURITY_HEADERS,
    overrides: Mapping[str, Override] | None = None,
) -> Iterator[str]:
    """Serve `root` until the block ends; yields the base URL (`http://127.0.0.1:<port>`)."""
    server = _Server(("127.0.0.1", 0), partial(_Handler, directory=str(root)))
    server.headers = dict(headers)
    server.overrides = dict(overrides or {})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host!s}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
