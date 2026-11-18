"""Tests for the build step (`scripts/build_site.py`)."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

import pytest

from scripts.build_site import BUILD_MARKER, BuildError, build_site, main
from scripts.page_parser import parse_page

HASHED = re.compile(r"^(?P<stem>.+)\.(?P<hash>[0-9a-f]{8})(?P<suffix>\.\w+)$")


@pytest.fixture
def source(tmp_path: Path, site_dir: Path) -> Path:
    """A private copy of `site/` that a test may change."""
    copy = tmp_path / "site"
    shutil.copytree(site_dir, copy)
    return copy


def snapshot(folder: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(folder)): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def test_assets_get_content_hashed_names(source: Path, tmp_path: Path) -> None:
    result = build_site(source, tmp_path / "dist")
    assert set(result.renamed) == {
        "/assets/styles.css",
        "/assets/raad-logo.webp",
        "/assets/icon-180.png",
        *(f"/assets/fonts/{p.name}" for p in (source / "assets" / "fonts").glob("*.woff2")),
    }
    for old, new in result.renamed.items():
        match = HASHED.match(Path(new).name)
        assert match, new
        assert f"{match['stem']}{match['suffix']}" == Path(old).name
        data = (result.out_dir / new.lstrip("/")).read_bytes()
        assert hashlib.sha256(data).hexdigest()[:8] == match["hash"]
        assert not (result.out_dir / old.lstrip("/")).exists()
    # The font licence and the favicon keep their fixed names.
    assert (result.out_dir / "assets" / "fonts" / "OFL.txt").is_file()
    assert (result.out_dir / "favicon.ico").is_file()


def test_pages_and_stylesheet_point_only_at_hashed_files_that_exist(
    source: Path, tmp_path: Path
) -> None:
    result = build_site(source, tmp_path / "dist")
    assert result.pages == ("404.html", "en/index.html", "index.html")
    for page_name in result.pages:
        page = parse_page((result.out_dir / page_name).read_text(encoding="utf-8"))
        assets = [url for _, _, url in page.url_references() if url.startswith("/assets/")]
        assert sorted(assets) == sorted(
            result.renamed[name]
            for name in ("/assets/styles.css", "/assets/raad-logo.webp", "/assets/icon-180.png")
        )
        for url in assets:
            assert (result.out_dir / url.lstrip("/")).is_file()
    css_name = result.renamed["/assets/styles.css"].lstrip("/")
    css = (result.out_dir / css_name).read_text(encoding="utf-8")
    font_urls = re.findall(r'url\("([^"]+)"\)', css)
    assert len(font_urls) == 4
    for url in font_urls:
        assert HASHED.match(Path(url).name), url
        assert (result.out_dir / "assets" / url).is_file()


def test_build_is_stable_when_nothing_changed(source: Path, tmp_path: Path) -> None:
    first = build_site(source, tmp_path / "one")
    second = build_site(source, tmp_path / "two")
    assert first.renamed == second.renamed
    assert snapshot(first.out_dir) == snapshot(second.out_dir)
    again = build_site(source, tmp_path / "one")  # rebuild in place
    assert snapshot(again.out_dir) == snapshot(second.out_dir)


def test_changing_a_font_renames_the_font_and_the_stylesheet(source: Path, tmp_path: Path) -> None:
    before = build_site(source, tmp_path / "before")
    font = next((source / "assets" / "fonts").glob("*latin-400*.woff2"))
    font.write_bytes(font.read_bytes() + b"\0")
    after = build_site(source, tmp_path / "after")
    font_url = f"/assets/fonts/{font.name}"
    assert after.renamed[font_url] != before.renamed[font_url]
    assert after.renamed["/assets/styles.css"] != before.renamed["/assets/styles.css"]
    assert after.renamed["/assets/raad-logo.webp"] == before.renamed["/assets/raad-logo.webp"]


def test_rebuild_removes_files_from_the_previous_build(source: Path, tmp_path: Path) -> None:
    out = tmp_path / "dist"
    first = build_site(source, out)
    (source / "assets" / "styles.css").write_text("body { color: #f2f4f8; }\n", encoding="utf-8")
    second = build_site(source, out)
    old_css = out / first.renamed["/assets/styles.css"].lstrip("/")
    assert not old_css.exists()
    assert (out / second.renamed["/assets/styles.css"].lstrip("/")).is_file()
    assert (out / BUILD_MARKER).is_file()


@pytest.mark.parametrize(
    ("page", "old", "new", "message"),
    [
        ("index.html", "/assets/raad-logo.webp", "/assets/missing.webp", "missing reference"),
        ("en/index.html", 'href="/"', 'href="/fr/"', "missing reference"),
        ("index.html", 'href="#main"', 'href="#nowhere"', "missing in-page target"),
        (
            "en/index.html",
            "/assets/styles.css",
            "https://cdn.example.com/styles.css",
            "root-relative",
        ),
    ],
)
def test_broken_page_reference_fails_and_writes_nothing(
    source: Path, tmp_path: Path, page: str, old: str, new: str, message: str
) -> None:
    path = source / page
    path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
    out = tmp_path / "dist"
    with pytest.raises(BuildError, match=message):
        build_site(source, out)
    assert not out.exists()


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ('url("fonts/missing.woff2")', "missing reference"),
        ('url("https://fonts.example.com/plex.woff2")', "only files in the site"),
    ],
)
def test_broken_stylesheet_reference_fails(
    source: Path, tmp_path: Path, replacement: str, message: str
) -> None:
    css = source / "assets" / "styles.css"
    text = css.read_text(encoding="utf-8")
    text = re.sub(r'url\("fonts/[^"]+latin-600[^"]+"\)', replacement, text, count=1)
    css.write_text(text, encoding="utf-8")
    with pytest.raises(BuildError, match=message):
        build_site(source, tmp_path / "dist")


def test_refuses_unsafe_output_folders(source: Path, tmp_path: Path) -> None:
    with pytest.raises(BuildError, match="outside the site folder"):
        build_site(source, source / "dist")
    with pytest.raises(BuildError, match="outside the site folder"):
        build_site(source, tmp_path)  # would contain the source
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "notes.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(BuildError, match="not made by this build"):
        build_site(source, unrelated)
    assert (unrelated / "notes.txt").read_text(encoding="utf-8") == "keep me"


def test_cli_reports_success_and_failure(
    source: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--source", str(source), "--out", str(tmp_path / "dist")]) == 0
    assert "/assets/styles.css -> /assets/styles." in capsys.readouterr().out
    assert main(["--source", str(tmp_path / "nowhere"), "--out", str(tmp_path / "x")]) == 1
    assert "build failed: site folder not found" in capsys.readouterr().err


def test_hashed_file_types_outside_assets_are_refused(source: Path, tmp_path: Path) -> None:
    # A fixed-name PNG at the root would be cached for a year under a name that never changes.
    shutil.copy(source / "assets" / "icon-180.png", source / "apple-touch-icon.png")
    with pytest.raises(BuildError, match="move these under assets/"):
        build_site(source, tmp_path / "dist")
