"""Smoke test for a deployed landing site (both languages).

Fetches `/` (Arabic) and `/en/` (English) over HTTPS with TLS 1.2 as the minimum and checks, for
each page: the status, `lang` and `dir`, the RAAD name, the description, both contact addresses,
that the hashed stylesheet loads, that the security headers are present, and that the page names
no host but its own (so it can make no call to a RAAD API or sign-in host). It also checks that
an unknown path answers 404 with the not-found page (not the bucket's XML error) and that the
favicon loads. Any failed check exits non-zero; an empty or missing page is never a pass.

It only reads public pages: it sends no data and collects nothing about visitors.

Usage: uv run python scripts/smoke_site.py https://dev.raadwallet.com [--attempts 3 --wait 20]
"""

from __future__ import annotations

import argparse
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from http.client import HTTPMessage
from typing import IO, Any
from urllib.parse import unquote, urljoin, urlsplit

from raadweb.content import DESCRIPTIONS, LOGO_ALT, PRODUCT_NAME, PUBLIC_CONTACTS
from raadweb.web_policy import (
    CONTENT_SECURITY_POLICY,
    CONTENT_TYPE_OPTIONS,
    HSTS_MAX_AGE_SECONDS,
    REFERRER_POLICY,
)
from scripts.page_parser import Page, parse_page

STYLESHEET = re.compile(r"^/assets/styles\.[0-9a-f]{8}\.css$")
TIMEOUT_SECONDS = 15
UNKNOWN_PATH = "/raadweb-smoke-no-such-page"


@dataclass(frozen=True)
class PageExpectation:
    path: str
    lang: str
    direction: str


PAGES = (
    PageExpectation(path="/", lang="ar", direction="rtl"),
    PageExpectation(path="/en/", lang="en", direction="ltr"),
)


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    headers: Mapping[str, str]  # lower-case names
    body: bytes


Fetcher = Callable[[str], Response]


@dataclass
class Report:
    base_url: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, ok, detail))
        return ok

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)

    def lines(self) -> list[str]:
        return [
            f"{'PASS' if ok else 'FAIL'}  {name}{f': {detail}' if detail else ''}"
            for name, ok, detail in self.checks
        ]


def tls_context() -> ssl.SSLContext:
    """Certificate-verifying context that refuses anything older than TLS 1.2."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to HTTPS URLs."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if urlsplit(newurl).scheme != "https":
            raise urllib.error.URLError(f"refused redirect away from HTTPS to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _to_response(handle: Any, url: str) -> Response:
    headers = {name.lower(): value for name, value in handle.headers.items()}
    return Response(url=url, status=int(handle.status), headers=headers, body=handle.read())


def make_fetcher(*, require_https: bool = True) -> Fetcher:
    """A fetcher over HTTPS (TLS 1.2+); plain HTTP only when a local test asks for it."""
    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.HTTPSHandler(context=tls_context())
    ]
    if require_https:
        handlers.append(HttpsOnlyRedirects())
    opener = urllib.request.build_opener(*handlers)

    def fetch(url: str) -> Response:
        if require_https and urlsplit(url).scheme != "https":
            raise urllib.error.URLError(f"only HTTPS is allowed: {url}")
        try:
            with opener.open(url, timeout=TIMEOUT_SECONDS) as handle:
                return _to_response(handle, url)
        except urllib.error.HTTPError as error:
            return _to_response(error, url)

    return fetch


def _check_headers(report: Report, label: str, headers: Mapping[str, str]) -> None:
    report.check(
        f"{label} Content-Security-Policy",
        headers.get("content-security-policy") == CONTENT_SECURITY_POLICY,
        headers.get("content-security-policy", "missing"),
    )
    hsts = headers.get("strict-transport-security", "")
    max_age = re.search(r"max-age=(\d+)", hsts)
    report.check(
        f"{label} Strict-Transport-Security",
        max_age is not None and int(max_age.group(1)) >= HSTS_MAX_AGE_SECONDS,
        hsts or "missing",
    )
    report.check(
        f"{label} X-Content-Type-Options",
        headers.get("x-content-type-options") == CONTENT_TYPE_OPTIONS,
        headers.get("x-content-type-options", "missing"),
    )
    report.check(
        f"{label} Referrer-Policy",
        headers.get("referrer-policy") == REFERRER_POLICY,
        headers.get("referrer-policy", "missing"),
    )


def _check_contacts(report: Report, label: str, page: Page) -> None:
    mailtos = [a.get("href") for a in page.find_all("a") if a.get("href").startswith("mailto:")]
    recipients = sorted(unquote(r) for r in urlsplit(mailtos[0]).path.split(",")) if mailtos else []
    report.check(
        f"{label} request-access link names both contacts",
        len(mailtos) == 1 and recipients == sorted(PUBLIC_CONTACTS),
        ", ".join(recipients) or "no mailto link",
    )
    shown = sorted(e.text for e in page.by_testid("landing-contact-address"))
    report.check(
        f"{label} both contact addresses are shown",
        shown == sorted(PUBLIC_CONTACTS),
        ", ".join(shown) or "none",
    )


def _check_own_host_only(report: Report, label: str, page: Page) -> None:
    foreign = [
        url
        for _, _, url in page.url_references()
        if not url.startswith("mailto:") and (urlsplit(url).scheme or urlsplit(url).netloc)
    ]
    report.check(
        f"{label} names no other host (no RAAD API or sign-in call)",
        not foreign,
        ", ".join(foreign),
    )


def _check_stylesheet(
    report: Report, label: str, page: Page, page_url: str, fetch: Fetcher
) -> None:
    hrefs = [link.get("href") for link in page.find_all("link") if link.get("rel") == "stylesheet"]
    if not report.check(
        f"{label} links one hashed stylesheet",
        len(hrefs) == 1 and bool(STYLESHEET.match(hrefs[0])),
        ", ".join(hrefs) or "none",
    ):
        return
    css = fetch(urljoin(page_url, hrefs[0]))
    report.check(
        f"{label} stylesheet loads",
        css.status == 200
        and css.headers.get("content-type", "").startswith("text/css")
        and len(css.body) > 0,
        f"{css.status} {css.headers.get('content-type', '')} {len(css.body)} bytes",
    )


def check_page(report: Report, expected: PageExpectation, fetch: Fetcher) -> None:
    url = urljoin(report.base_url, expected.path)
    label = expected.path
    response = fetch(url)
    content_type = response.headers.get("content-type", "")
    if not report.check(
        f"{label} answers 200 with a page",
        response.status == 200 and content_type.startswith("text/html") and len(response.body) > 0,
        f"{response.status} {content_type} {len(response.body)} bytes",
    ):
        return
    page = parse_page(response.body.decode("utf-8", errors="replace"))
    html = page.find("html")
    lang, direction = (html.get("lang"), html.get("dir")) if html else ("", "")
    report.check(
        f"{label} lang and dir",
        (lang, direction) == (expected.lang, expected.direction),
        f"lang={lang} dir={direction}",
    )
    title = page.find("title")
    title_text = title.text if title else ""
    report.check(
        f"{label} shows the RAAD name",
        PRODUCT_NAME in title_text
        and LOGO_ALT in [img.attrs.get("alt") for img in page.find_all("img")],
        title_text or "no title",
    )
    descriptions = [e.text for e in page.by_testid("landing-description")]
    report.check(
        f"{label} description",
        descriptions == [DESCRIPTIONS[expected.lang]],
        " / ".join(descriptions) or "none",
    )
    _check_contacts(report, label, page)
    _check_own_host_only(report, label, page)
    _check_headers(report, label, response.headers)
    _check_stylesheet(report, label, page, url, fetch)


def check_not_found(report: Report, fetch: Fetcher) -> None:
    """An unknown path must show the bilingual not-found page with a real 404, not S3's XML."""
    response = fetch(urljoin(report.base_url, UNKNOWN_PATH.lstrip("/")))
    content_type = response.headers.get("content-type", "")
    page = parse_page(response.body.decode("utf-8", errors="replace"))
    report.check(
        f"{UNKNOWN_PATH} answers 404 with the not-found page",
        response.status == 404
        and content_type.startswith("text/html")
        and bool(page.by_testid("landing-not-found")),
        f"{response.status} {content_type} {len(response.body)} bytes",
    )


def check_favicon(report: Report, fetch: Fetcher) -> None:
    response = fetch(urljoin(report.base_url, "favicon.ico"))
    content_type = response.headers.get("content-type", "")
    report.check(
        "/favicon.ico loads",
        response.status == 200 and content_type.startswith("image/") and len(response.body) > 0,
        f"{response.status} {content_type} {len(response.body)} bytes",
    )


def run_smoke(base_url: str, fetch: Fetcher, *, require_https: bool = True) -> Report:
    """Check the site under `base_url`. A site problem becomes a failed check, not an error."""
    base = base_url.rstrip("/") + "/"
    report = Report(base_url=base)
    if require_https and urlsplit(base).scheme != "https":
        report.check("base URL uses HTTPS", False, base)
        return report
    checks: list[tuple[str, Callable[[], None]]] = [
        *((p.path, partial(check_page, report, p, fetch)) for p in PAGES),
        (UNKNOWN_PATH, partial(check_not_found, report, fetch)),
        ("/favicon.ico", partial(check_favicon, report, fetch)),
    ]
    for path, check in checks:
        try:
            check()
        except (urllib.error.URLError, OSError, ValueError) as error:
            report.check(f"{path} can be fetched", False, str(error))
    return report


def main(
    argv: Sequence[str] | None = None,
    *,
    fetch: Fetcher | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(description="Smoke test for a deployed landing site.")
    parser.add_argument("base_url", help="for example https://dev.raadwallet.com")
    parser.add_argument("--attempts", type=int, default=3, help="tries before failing")
    parser.add_argument("--wait", type=float, default=20.0, help="seconds between tries")
    args = parser.parse_args(argv)
    fetcher = fetch or make_fetcher()
    attempts = max(args.attempts, 1)
    for attempt in range(1, attempts + 1):
        report = run_smoke(args.base_url, fetcher)
        if report.passed or attempt == attempts:
            break
        print(
            f"attempt {attempt} of {attempts} failed; retrying in {args.wait:g}s", file=sys.stderr
        )
        sleep(args.wait)
    print(f"Smoke test of {report.base_url}")
    print("\n".join(report.lines()))
    print("RESULT: PASS" if report.passed else "RESULT: FAIL")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
