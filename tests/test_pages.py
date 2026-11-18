"""Content, structure and safety checks for the two landing pages (S12) and their stylesheet.

These are parser checks on the source in `site/`. They do not replace the browser scan in
`tests/browser/` or the manual keyboard, screen-reader and native-Arabic reviews.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from scripts.page_parser import Page, parse_page

CONTACTS = ("abdulaziz.alrubai@outlook.com", "zyad.alamri777@gmail.com")
LOGO_ALT = "رعد RAAD"
FOCUSABLE = {"a", "button", "input", "select", "textarea", "summary"}
FORBIDDEN_ELEMENTS = {
    "script",
    "noscript",
    "style",
    "form",
    "input",
    "button",
    "select",
    "textarea",
    "iframe",
    "frame",
    "object",
    "embed",
    "base",
}
STORE_HOSTS = ("apps.apple.com", "itunes.apple.com", "testflight", "play.google.com")


@dataclass(frozen=True)
class Expected:
    path: str
    lang: str
    direction: str
    h1: str
    title_part: str
    description: str
    notice: str
    subject: str
    skip_text: str
    nav_label: str
    other_href: str
    other_hreflang: str


EXPECTED = {
    "ar": Expected(
        path="index.html",
        lang="ar",
        direction="rtl",
        h1="أرسل المال في ثوانٍ.",
        title_part="أرسل المال في ثوانٍ",
        description="رعد محفظة تجريبية لإرسال مبالغ افتراضية بالريال السعودي بين مستخدمي رعد.",
        notice="الأموال في رعد افتراضية، ولا تُعالَج أي مدفوعات حقيقية.",
        subject="طلب وصول تجريبي إلى رعد",
        skip_text="تخطَّ إلى المحتوى",
        nav_label="اللغة",
        other_href="/en/",
        other_hreflang="en",
    ),
    "en": Expected(
        path="en/index.html",
        lang="en",
        direction="ltr",
        h1="Send money in seconds.",
        title_part="Send money in seconds",
        description=(
            "RAAD is a demo wallet for sending simulated Saudi riyals between RAAD users."
        ),
        notice="money in RAAD is simulated, and no real payments are processed.",
        subject="RAAD test access request",
        skip_text="Skip to content",
        nav_label="Language",
        other_href="/",
        other_hreflang="ar",
    ),
}
LANGS = sorted(EXPECTED)
NOT_FOUND = "404.html"
PAGE_FILES = [EXPECTED["ar"].path, EXPECTED["en"].path, NOT_FOUND]


def load_file(site_dir: Path, name: str) -> tuple[str, Page]:
    raw = (site_dir / name).read_text(encoding="utf-8")
    return raw, parse_page(raw)


def load(site_dir: Path, lang: str) -> tuple[str, Page]:
    return load_file(site_dir, EXPECTED[lang].path)


def resolve_local(site_dir: Path, url: str) -> Path:
    """Map a root-relative URL to the file the static host serves for it."""
    path = unquote(urlsplit(url).path).lstrip("/")
    target = site_dir / path
    return target / "index.html" if url.endswith("/") or target.is_dir() else target


@pytest.mark.parametrize("lang", LANGS)
def test_document_language_direction_and_head(site_dir: Path, lang: str) -> None:
    _, page = load(site_dir, lang)
    html = page.find("html")
    assert html is not None
    assert html.get("lang") == EXPECTED[lang].lang
    assert html.get("dir") == EXPECTED[lang].direction
    metas = {m.get("name"): m.get("content") for m in page.find_all("meta") if m.get("name")}
    assert any(m.get("charset").lower() == "utf-8" for m in page.find_all("meta"))
    assert metas["viewport"] == "width=device-width, initial-scale=1"
    assert metas["description"] == EXPECTED[lang].description


@pytest.mark.parametrize("lang", LANGS)
def test_title_names_raad_and_page_has_one_h1(site_dir: Path, lang: str) -> None:
    _, page = load(site_dir, lang)
    title = page.find("title")
    assert title is not None
    assert "RAAD" in title.text
    assert EXPECTED[lang].title_part in title.text
    h1s = page.find_all("h1")
    assert [h.text for h in h1s] == [EXPECTED[lang].h1]


@pytest.mark.parametrize("lang", LANGS)
def test_skip_link_is_first_focusable_and_its_target_exists(site_dir: Path, lang: str) -> None:
    _, page = load(site_dir, lang)
    focusable = [e for e in page.elements if e.tag in FOCUSABLE or "tabindex" in e.attrs]
    first = next(e for e in focusable if e.get("tabindex") != "-1")
    assert first.tag == "a"
    assert first.get("href") == "#main"
    assert first.text == EXPECTED[lang].skip_text
    target = page.by_id("main")
    assert target is not None
    assert target.tag == "main"


@pytest.mark.parametrize("lang", LANGS)
def test_landmarks_and_named_language_navigation(site_dir: Path, lang: str) -> None:
    _, page = load(site_dir, lang)
    for landmark in ("header", "main", "footer"):
        assert len(page.find_all(landmark)) == 1, landmark
    navs = page.find_all("nav")
    assert len(navs) == 1
    assert navs[0].get("aria-label") == EXPECTED[lang].nav_label
    footer = page.find("footer")
    assert footer is not None
    assert footer.text == "© 2026 RAAD"


@pytest.mark.parametrize("lang", LANGS)
def test_language_links_switch_to_the_other_page_with_hreflang(site_dir: Path, lang: str) -> None:
    _, page = load(site_dir, lang)
    links = {a.get("hreflang"): a for a in page.find_all("a") if a.get("hreflang")}
    assert set(links) == {"ar", "en"}
    assert links["ar"].get("href") == "/"
    assert links["en"].get("href") == "/en/"
    assert links["ar"].get("lang") == "ar"
    assert links["en"].get("lang") == "en"
    assert links[lang].get("aria-current") == "page"
    assert "aria-current" not in links[EXPECTED[lang].other_hreflang].attrs
    assert links[EXPECTED[lang].other_hreflang].get("href") == EXPECTED[lang].other_href


@pytest.mark.parametrize("lang", LANGS)
def test_request_access_mailto_names_exactly_the_two_contacts(site_dir: Path, lang: str) -> None:
    _, page = load(site_dir, lang)
    mailtos = [a for a in page.find_all("a") if a.get("href").startswith("mailto:")]
    assert len(mailtos) == 1
    link = mailtos[0]
    assert link.get("data-testid") == "landing-request-access-link"
    target = urlsplit(link.get("href"))
    recipients = [unquote(r) for r in target.path.split(",")]
    assert sorted(recipients) == sorted(CONTACTS)
    query = dict(part.split("=", 1) for part in target.query.split("&"))
    assert set(query) == {"subject"}
    assert unquote(query["subject"]) == EXPECTED[lang].subject


@pytest.mark.parametrize("lang", LANGS)
def test_both_contact_addresses_are_visible_and_isolated_ltr(site_dir: Path, lang: str) -> None:
    _, page = load(site_dir, lang)
    shown = page.by_testid("landing-contact-address")
    assert sorted(e.text for e in shown) == sorted(CONTACTS)
    for element in shown:
        assert element.tag == "bdi"
        assert element.get("dir") == "ltr"
    for contact in CONTACTS:
        assert contact in page.body_text


@pytest.mark.parametrize("lang", LANGS)
def test_logo_is_local_and_carries_the_name_as_alt_text(site_dir: Path, lang: str) -> None:
    _, page = load(site_dir, lang)
    images = page.find_all("img")
    assert len(images) == 1
    logo = images[0]
    src = logo.get("src")
    assert src.startswith("/assets/")
    assert not urlsplit(src).netloc
    assert resolve_local(site_dir, src).is_file()
    assert logo.attrs.get("alt") == LOGO_ALT
    assert logo.get("width").isdigit() and logo.get("height").isdigit()
    header = page.find("header")
    assert header is not None and header.index < logo.index


@pytest.mark.parametrize("lang", LANGS)
def test_description_and_simulated_money_notice(site_dir: Path, lang: str) -> None:
    _, page = load(site_dir, lang)
    [description] = page.by_testid("landing-description")
    assert description.text == EXPECTED[lang].description
    [notice] = page.by_testid("landing-demo-notice")
    assert EXPECTED[lang].notice in notice.text
    # The status is carried by a text label, not by colour alone.
    assert page.find("strong") is not None
    assert "sent" not in page.body_text.lower()  # never claims a request was sent


@pytest.mark.parametrize("page_file", PAGE_FILES)
def test_every_internal_link_and_asset_resolves(site_dir: Path, page_file: str) -> None:
    _, page = load_file(site_dir, page_file)
    checked = 0
    for element, _name, url in page.url_references():
        if url.startswith("mailto:"):
            continue
        if url.startswith("#"):
            assert page.by_id(url[1:]) is not None, url
            continue
        assert url.startswith("/"), f"{element.tag} uses a non-root-relative URL: {url}"
        assert resolve_local(site_dir, url).is_file(), url
        checked += 1
    assert checked >= 4  # stylesheet, logo and both language links


@pytest.mark.parametrize("page_file", PAGE_FILES)
def test_no_scripts_forms_frames_inline_styles_or_handlers(site_dir: Path, page_file: str) -> None:
    raw, page = load_file(site_dir, page_file)
    found = {e.tag for e in page.elements} & FORBIDDEN_ELEMENTS
    assert not found
    for element in page.elements:
        handlers = [name for name in element.attrs if name.startswith("on")]
        assert not handlers, (element.tag, handlers)
        assert "style" not in element.attrs, element.tag  # CSP: style-src 'self' only
        assert "http-equiv" not in element.attrs, element.tag
    assert "javascript:" not in raw.lower()


@pytest.mark.parametrize("page_file", PAGE_FILES)
def test_no_external_resources_store_links_or_other_addresses(
    site_dir: Path, page_file: str
) -> None:
    raw, page = load_file(site_dir, page_file)
    for element, name, url in page.url_references():
        if url.startswith("mailto:"):
            continue
        parts = urlsplit(url)
        assert not parts.scheme and not parts.netloc, f"external {name} on {element.tag}: {url}"
    lowered = raw.lower()
    for host in STORE_HOSTS:
        assert host not in lowered
    addresses = set(re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", raw))
    assert addresses <= set(CONTACTS)  # the landing pages show both (tested above)


@pytest.mark.parametrize("lang", LANGS)
def test_interactive_elements_have_stable_test_ids(site_dir: Path, lang: str) -> None:
    _, page = load(site_dir, lang)
    ids = [a.get("data-testid") for a in page.find_all("a")]
    assert all(ids), ids
    assert ids == [
        "landing-skip-link",
        "landing-language-ar",
        "landing-language-en",
        "landing-request-access-link",
    ]


def test_stylesheet_has_no_import_and_only_local_urls(site_dir: Path) -> None:
    css_path = site_dir / "assets" / "styles.css"
    css = css_path.read_text(encoding="utf-8")
    assert "@import" not in css
    urls = re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", css)
    assert len(urls) == 4  # the four bundled font files
    for url in urls:
        parts = urlsplit(url)
        assert not parts.scheme and not parts.netloc and not url.startswith("//"), url
        assert (css_path.parent / url).resolve().is_file(), url
    assert "expression(" not in css.lower()


def test_stylesheet_layout_focus_motion_and_logo_rules(site_dir: Path) -> None:
    css = (site_dir / "assets" / "styles.css").read_text(encoding="utf-8")
    assert "--column: 720px" in css
    assert "--gutter: 16px" in css
    assert re.search(r"a:focus-visible\s*{[^}]*outline: 2px solid var\(--accent\)", css)
    assert re.search(r"\.button\s*{[^}]*min-block-size: 48px", css)
    assert re.search(r"\.brand-logo\s*{[^}]*inline-size: 240px[^}]*mix-blend-mode: screen", css)
    assert re.search(
        r"@media \(max-width: 480px\)\s*{\s*\.brand-logo\s*{\s*inline-size: 200px", css
    )
    assert "@media (prefers-reduced-motion: no-preference)" in css
    # Physical left/right would not mirror on the Arabic page.
    assert not re.search(r"(margin|padding)-(left|right)|text-align:\s*(left|right)", css)
    for token in ("#0a0c10", "#12151b", "#f2f4f8", "#aeb6c4", "#5ba8ff", "#06121f"):
        assert token in css


@pytest.mark.parametrize("page_file", PAGE_FILES)
def test_every_page_links_the_favicon_and_touch_icon(site_dir: Path, page_file: str) -> None:
    _, page = load_file(site_dir, page_file)
    links = {link.get("rel"): link for link in page.find_all("link")}
    assert links["icon"].get("href") == "/favicon.ico"
    assert links["icon"].get("sizes") == "32x32"
    assert links["apple-touch-icon"].get("href") == "/assets/icon-180.png"
    for rel in ("icon", "apple-touch-icon"):
        assert resolve_local(site_dir, links[rel].get("href")).is_file()


def test_favicon_and_touch_icon_have_the_declared_sizes(site_dir: Path) -> None:
    ico = (site_dir / "favicon.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", ico[:6])
    assert (reserved, kind, count) == (0, 1, 1)  # one icon image
    assert (ico[6], ico[7]) == (32, 32)
    png = (site_dir / "assets" / "icon-180.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (180, 180)
    assert len(ico) < 10_000 and len(png) < 50_000


def test_not_found_page_is_arabic_first_then_english(site_dir: Path) -> None:
    _, page = load_file(site_dir, NOT_FOUND)
    html = page.find("html")
    assert html is not None
    assert (html.get("lang"), html.get("dir")) == ("ar", "rtl")
    sections = page.find_all("section")
    assert len(sections) == 2
    arabic, english = sections
    assert "lang" not in arabic.attrs  # inherits Arabic from the page
    assert (english.get("lang"), english.get("dir")) == ("en", "ltr")
    assert arabic.index < english.index
    assert [h.text for h in page.find_all("h1")] == ["الصفحة غير موجودة"]
    assert [h.text for h in page.find_all("h2")] == ["Page not found"]
    assert "page not found" in english.text.lower()
    title = page.find("title")
    assert title is not None
    assert "RAAD" in title.text and "Page not found" in title.text
    assert "الصفحة غير موجودة" in title.text


def test_not_found_page_is_not_indexed_and_links_home_in_both_languages(site_dir: Path) -> None:
    _, page = load_file(site_dir, NOT_FOUND)
    metas = {m.get("name"): m.get("content") for m in page.find_all("meta") if m.get("name")}
    assert metas["robots"] == "noindex"
    assert not [link for link in page.find_all("link") if link.get("rel") == "canonical"]
    [main] = page.find_all("main")
    assert main.get("id") == "main"
    assert main.get("data-testid") == "landing-not-found"
    home = {a.get("data-testid"): a for a in page.find_all("a") if a.index > main.index}
    assert home["landing-home-ar"].get("href") == "/"
    assert home["landing-home-en"].get("href") == "/en/"
    for landmark in ("header", "main", "footer"):
        assert len(page.find_all(landmark)) == 1, landmark
    [logo] = page.find_all("img")
    assert logo.attrs.get("alt") == LOGO_ALT


def test_not_found_page_keyboard_entry_and_test_ids(site_dir: Path) -> None:
    _, page = load_file(site_dir, NOT_FOUND)
    links = page.find_all("a")
    assert links[0].get("href") == "#main"
    assert [a.get("data-testid") for a in links] == [
        "landing-skip-link",
        "landing-language-ar",
        "landing-language-en",
        "landing-home-ar",
        "landing-home-en",
    ]
    assert "aria-current" not in " ".join(" ".join(a.attrs) for a in links)


@pytest.mark.parametrize("lang", LANGS)
def test_head_declares_canonical_and_language_alternates(site_dir: Path, lang: str) -> None:
    _, page = load(site_dir, lang)
    head = page.find("head")
    body = page.find("body")
    assert head is not None and body is not None
    links = [link for link in page.find_all("link") if head.index < link.index < body.index]
    canonical = [link.get("href") for link in links if link.get("rel") == "canonical"]
    assert canonical == ["/" if lang == "ar" else "/en/"]
    alternates = {
        link.get("hreflang"): link.get("href") for link in links if link.get("rel") == "alternate"
    }
    assert alternates == {"ar": "/", "en": "/en/", "x-default": "/"}
    # Root-relative on purpose: the same build is promoted from dev to stg to prd.
    for href in [*canonical, *alternates.values()]:
        parts = urlsplit(href)
        assert not parts.scheme and not parts.netloc, href
        assert resolve_local(site_dir, href).is_file(), href
