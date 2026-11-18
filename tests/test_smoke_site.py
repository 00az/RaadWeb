"""Tests for the deployed-site smoke test (`scripts/smoke_site.py`).

The site is served from a local build on 127.0.0.1 (plain HTTP, allowed only through the test
hook), and the HTTPS/TLS behaviour is checked with stubbed responses. No real host is contacted.
"""

from __future__ import annotations

import shutil
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from http.client import HTTPMessage
from io import BytesIO
from pathlib import Path

import pytest

from raadweb.web_policy import CONTENT_TYPES, SECURITY_HEADERS
from scripts.build_site import build_site
from scripts.smoke_site import (
    UNKNOWN_PATH,
    HttpsOnlyRedirects,
    Response,
    make_fetcher,
    run_smoke,
    tls_context,
)
from scripts.smoke_site import main as smoke_main
from tests.site_server import Override, serve

LOCAL = make_fetcher(require_https=False)


@pytest.fixture
def dist(tmp_path: Path, site_dir: Path) -> Path:
    return build_site(site_dir, tmp_path / "dist").out_dir


def failed(report_lines: list[str]) -> list[str]:
    return [line for line in report_lines if line.startswith("FAIL")]


def stub_fetcher(dist: Path, base: str = "https://raad.example.test") -> Callable[[str], Response]:
    """Answer like the deployed site, from the local build, without any network.

    A miss gets the build's 404 page with status 404, as the distribution's error responses
    do; with no 404 page in the build it gets the bucket's raw 403 XML.
    """

    def fetch(url: str) -> Response:
        path = url.removeprefix(base)
        file = dist / path.lstrip("/")
        if path.endswith("/"):
            file = file / "index.html"
        headers = {name.lower(): value for name, value in SECURITY_HEADERS.items()}
        status = 200
        if not file.is_file():
            if not (dist / "404.html").is_file():
                return Response(url, 403, {"content-type": "application/xml"}, b"AccessDenied")
            file, status = dist / "404.html", 404
        headers["content-type"] = CONTENT_TYPES[file.suffix]
        return Response(url, status, headers, file.read_bytes())

    return fetch


def test_correct_build_passes_every_check_on_both_pages(dist: Path) -> None:
    with serve(dist) as base:
        report = run_smoke(base, LOCAL, require_https=False)
    assert report.passed, failed(report.lines())
    names = [name for name, _, _ in report.checks]
    for page in ("/", "/en/"):
        assert f"{page} lang and dir" in names
        assert f"{page} stylesheet loads" in names
        assert f"{page} request-access link names both contacts" in names
    assert f"{UNKNOWN_PATH} answers 404 with the not-found page" in names
    assert "/favicon.ico loads" in names
    assert len(names) == 2 * 13 + 2


def test_missing_page_fails(dist: Path) -> None:
    (dist / "en" / "index.html").unlink()
    with serve(dist) as base:
        report = run_smoke(base, LOCAL, require_https=False)
    assert not report.passed
    assert any(
        line.startswith("FAIL  /en/ answers 200 with a page: 404") for line in report.lines()
    )


def test_empty_page_is_never_a_pass(dist: Path) -> None:
    with serve(dist, overrides={"/": Override(status=200, body=b"")}) as base:
        report = run_smoke(base, LOCAL, require_https=False)
    assert not report.passed
    assert "FAIL  / answers 200 with a page: 200 text/html; charset=utf-8 0 bytes" in report.lines()


def test_wrong_contact_fails(dist: Path) -> None:
    page = dist / "index.html"
    text = page.read_text(encoding="utf-8").replace(
        "zyad.alamri777@gmail.com", "someone@example.com"
    )
    page.write_text(text, encoding="utf-8")
    with serve(dist) as base:
        report = run_smoke(base, LOCAL, require_https=False)
    failures = failed(report.lines())
    assert any("/ request-access link names both contacts" in line for line in failures)
    assert any("/ both contact addresses are shown" in line for line in failures)
    assert not any(line.startswith("FAIL  /en/") for line in failures)


@pytest.mark.parametrize(
    "header", ["Content-Security-Policy", "Strict-Transport-Security", "X-Content-Type-Options"]
)
def test_missing_security_header_fails(dist: Path, header: str) -> None:
    headers = {k: v for k, v in SECURITY_HEADERS.items() if k != header}
    with serve(dist, headers=headers) as base:
        report = run_smoke(base, LOCAL, require_https=False)
    assert f"FAIL  / {header}: missing" in report.lines()
    assert f"FAIL  /en/ {header}: missing" in report.lines()


def test_reference_to_another_host_and_missing_stylesheet_fail(dist: Path) -> None:
    page = dist / "en" / "index.html"
    text = page.read_text(encoding="utf-8").replace(
        "</main>", '<img src="https://api.raad.example.test/money/balance" alt=""></main>'
    )
    page.write_text(text, encoding="utf-8")
    for css in (dist / "assets").glob("styles.*.css"):
        css.unlink()
    with serve(dist) as base:
        report = run_smoke(base, LOCAL, require_https=False)
    failures = failed(report.lines())
    assert any(
        "/en/ names no other host" in line and "api.raad.example.test" in line for line in failures
    )
    assert any(line.startswith("FAIL  / stylesheet loads: 404") for line in failures)


def test_plain_http_is_refused_and_tls_12_is_the_minimum() -> None:
    report = run_smoke("http://raad.example.test", make_fetcher())
    assert report.lines() == ["FAIL  base URL uses HTTPS: http://raad.example.test/"]
    with pytest.raises(urllib.error.URLError, match="only HTTPS"):
        make_fetcher()("http://raad.example.test/")
    context = tls_context()
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_redirects_may_not_leave_https() -> None:
    handler = HttpsOnlyRedirects()
    request = urllib.request.Request("https://raad.example.test/en")
    with pytest.raises(urllib.error.URLError, match="refused redirect"):
        handler.redirect_request(
            request, BytesIO(), 301, "Moved", HTTPMessage(), "http://raad.example.test/en/"
        )
    allowed = handler.redirect_request(
        request, BytesIO(), 301, "Moved", HTTPMessage(), "https://raad.example.test/en/"
    )
    assert allowed is not None and allowed.full_url == "https://raad.example.test/en/"


def test_cli_retries_then_passes_against_stubbed_https_responses(
    dist: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = stub_fetcher(dist)
    calls = {"n": 0}

    def flaky(url: str) -> Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("temporary DNS failure")
        return good(url)

    waits: list[float] = []
    code = smoke_main(
        ["https://raad.example.test", "--attempts", "2", "--wait", "7"],
        fetch=flaky,
        sleep=waits.append,
    )
    out = capsys.readouterr()
    assert code == 0
    assert waits == [7]
    assert "RESULT: PASS" in out.out
    assert "attempt 1 of 2 failed" in out.err


def test_cli_fails_when_the_site_is_down(dist: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shutil.rmtree(dist)
    dist.mkdir()
    code = smoke_main(
        ["https://raad.example.test", "--attempts", "1"],
        fetch=stub_fetcher(dist),
        sleep=lambda _: None,
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL  / answers 200 with a page: 403" in out
    assert "RESULT: FAIL" in out


def test_unknown_path_answered_with_bucket_xml_fails(dist: Path) -> None:
    raw_s3_error = Override(403, b"<Error><Code>AccessDenied</Code></Error>", "application/xml")
    with serve(dist, overrides={UNKNOWN_PATH: raw_s3_error}) as base:
        report = run_smoke(base, LOCAL, require_https=False)
    assert failed(report.lines()) == [
        f"FAIL  {UNKNOWN_PATH} answers 404 with the not-found page: "
        f"403 application/xml {len(raw_s3_error.body)} bytes"
    ]


def test_missing_favicon_fails(dist: Path) -> None:
    (dist / "favicon.ico").unlink()
    with serve(dist) as base:
        report = run_smoke(base, LOCAL, require_https=False)
    failures = failed(report.lines())
    assert len(failures) == 1
    assert failures[0].startswith("FAIL  /favicon.ico loads: 404 text/html")
