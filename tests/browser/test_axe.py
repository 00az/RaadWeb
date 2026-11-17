"""Automated accessibility and behaviour scan of the pages in headless Chromium.

Runs axe-core (WCAG 2.0/2.1 A and AA rules) on the built site served locally with the production
security headers: both language pages and the not-found page (an unknown path, answered with
`404.html` and status 404). It checks reflow at 320 px (also with text at 200%), keyboard entry
through the skip link, a visible focus ring, the language switch with JavaScript off, a missing
logo, and that a page loads nothing from any other host.

An automated scan finds only part of the WCAG problems; the manual keyboard, screen-reader and
native-Arabic reviews are still required before release.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from axe_playwright_python.sync_playwright import Axe  # type: ignore[import-untyped]
from playwright.sync_api import Browser, Page, Route

pytestmark = pytest.mark.browser

PAGES = {"/": ("ar", "rtl"), "/en/": ("en", "ltr")}
NOT_FOUND_PATH = "/raadweb-no-such-page"
# Every page the scan covers: path -> (lang, dir, HTTP status).
ALL_PAGES = {
    "/": ("ar", "rtl", 200),
    "/en/": ("en", "ltr", 200),
    NOT_FOUND_PATH: ("ar", "rtl", 404),
}
WCAG_21_AA = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]
LOGO_ALT = "رعد RAAD"


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    tab = context.new_page()
    yield tab
    context.close()


def has_horizontal_scroll(page: Page) -> bool:
    result: bool = page.evaluate(
        "() => document.documentElement.scrollWidth > document.documentElement.clientWidth"
    )
    return result


def describe(violations: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{v['id']} ({v['impact']}): {[n['target'] for n in v['nodes']]}" for v in violations
    )


@pytest.mark.parametrize("path", list(ALL_PAGES))
def test_no_wcag_21_aa_violations(page: Page, base_url: str, path: str) -> None:
    response = page.goto(base_url + path)
    page.wait_for_load_state("networkidle")
    assert response is not None and response.status == ALL_PAGES[path][2]
    results = Axe().run(
        page,
        options={"runOnly": {"type": "tag", "values": WCAG_21_AA}, "resultTypes": ["violations"]},
    )
    violations = results.response["violations"]
    assert violations == [], describe(violations)
    lang, direction, _ = ALL_PAGES[path]
    assert page.locator("html").get_attribute("lang") == lang
    assert page.locator("html").get_attribute("dir") == direction


@pytest.mark.parametrize("path", list(ALL_PAGES))
def test_reflows_at_320_px_and_with_text_at_200_percent(
    browser: Browser, base_url: str, path: str
) -> None:
    context = browser.new_context(viewport={"width": 320, "height": 640})
    page = context.new_page()
    try:
        page.goto(base_url + path)
        page.wait_for_load_state("networkidle")
        assert not has_horizontal_scroll(page)
        page.evaluate("() => { document.documentElement.style.fontSize = '200%'; }")
        assert not has_horizontal_scroll(page)
        # Every paragraph, list item and link in the content stays inside the 320 px viewport.
        content = page.locator("main p, main li, main a").all()
        assert content
        for element in content:
            box = element.bounding_box()
            assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 320
        logo = page.get_by_test_id("landing-logo").bounding_box()
        assert logo is not None and logo["width"] == 200
    finally:
        context.close()


@pytest.mark.parametrize("path", list(ALL_PAGES))
def test_keyboard_enters_through_the_skip_link_with_a_visible_focus_ring(
    page: Page, base_url: str, path: str
) -> None:
    page.goto(base_url + path)
    page.keyboard.press("Tab")
    focused = page.evaluate("() => document.activeElement.dataset.testid")
    assert focused == "landing-skip-link"
    skip = page.get_by_test_id("landing-skip-link").bounding_box()
    assert skip is not None and skip["y"] >= 0  # moved on screen when focused
    page.keyboard.press("Enter")
    assert page.evaluate("() => document.activeElement.id") == "main"
    page.keyboard.press("Tab")
    focused = page.evaluate("() => document.activeElement.dataset.testid")
    expected = "landing-home-ar" if path == NOT_FOUND_PATH else "landing-request-access-link"
    assert focused == expected
    outline = page.evaluate(
        "() => { const s = getComputedStyle(document.activeElement);"
        " return [s.outlineStyle, s.outlineWidth]; }"
    )
    assert outline == ["solid", "2px"]


@pytest.mark.parametrize("path", list(ALL_PAGES))
def test_loads_only_from_its_own_origin_without_csp_violations(
    page: Page, base_url: str, path: str
) -> None:
    requests: list[str] = []
    console: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    page.on("console", lambda message: console.append(message.text))
    response = page.goto(base_url + path)
    page.wait_for_load_state("networkidle")
    assert response is not None and response.status == ALL_PAGES[path][2]
    assert requests and all(url.startswith(base_url + "/") for url in requests), requests
    assert any(url.endswith(".css") for url in requests)
    assert any(url.endswith(".woff2") for url in requests)
    # Headless Chromium does not fetch favicons itself, so ask for it directly.
    favicon = page.request.get(base_url + "/favicon.ico")
    assert favicon.status == 200 and favicon.headers["content-type"] == "image/x-icon"
    assert not [text for text in console if "Content Security Policy" in text], console
    loaded = page.evaluate("() => document.querySelector('img.brand-logo').naturalWidth")
    assert loaded == 720
    blend = page.evaluate(
        "() => getComputedStyle(document.querySelector('img.brand-logo')).mixBlendMode"
    )
    assert blend == "screen"
    logo = page.get_by_test_id("landing-logo").bounding_box()
    assert logo is not None and logo["width"] == 240


@pytest.mark.parametrize("path", list(PAGES))
def test_language_switch_works_with_javascript_off(
    browser: Browser, base_url: str, path: str
) -> None:
    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    try:
        page.goto(base_url + path)
        other = "/en/" if path == "/" else "/"
        testid = "landing-language-en" if other == "/en/" else "landing-language-ar"
        page.get_by_test_id(testid).click()
        page.wait_for_url(base_url + other)
        lang, direction = PAGES[other]
        assert page.locator("html").get_attribute("lang") == lang
        assert page.locator("html").get_attribute("dir") == direction
    finally:
        context.close()


@pytest.mark.parametrize("path", list(PAGES))
def test_missing_logo_still_names_raad(page: Page, base_url: str, path: str) -> None:
    def block_logo(route: Route) -> None:
        route.abort()

    page.route("**/raad-logo.*.webp", block_logo)
    page.goto(base_url + path)
    page.wait_for_load_state("networkidle")
    assert page.evaluate("() => document.querySelector('img.brand-logo').naturalWidth") == 0
    assert page.get_by_role("img", name=LOGO_ALT).is_visible()
    assert "RAAD" in page.title()
    assert page.get_by_test_id("landing-request-access-link").is_visible()
