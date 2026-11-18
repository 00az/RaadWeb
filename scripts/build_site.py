"""Build the deployable landing site.

Copies `site/` to an output folder (normally `dist/`), gives every stylesheet, font and image
under `assets/` a content-hashed name (for example `styles.3f2a9c1b.css`) and rewrites the
references to them in the stylesheet and in both pages. Hashed names let CloudFront and browsers
cache assets for a year while a new deploy is picked up at once.

The build fails, and writes nothing, when a page or the stylesheet refers to a file that does not
exist or to anything outside the site.

Usage: uv run python scripts/build_site.py --out dist [--source site]
"""

from __future__ import annotations

import argparse
import hashlib
import posixpath
import re
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from raadweb.web_policy import HASHED_SUFFIXES
from scripts.page_parser import parse_page

REPO_ROOT = Path(__file__).resolve().parent.parent
HASH_LENGTH = 8
BUILD_MARKER = ".raadweb-build"
CSS_URL = re.compile(r"url\(\s*(['\"]?)([^'\")\s]+)\1\s*\)")


class BuildError(Exception):
    """The site cannot be built as it is."""


@dataclass(frozen=True)
class BuildResult:
    out_dir: Path
    renamed: dict[str, str]
    pages: tuple[str, ...]
    files: tuple[str, ...]


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:HASH_LENGTH]


def hashed_path(path: PurePosixPath, data: bytes) -> PurePosixPath:
    return path.with_name(f"{path.stem}.{content_hash(data)}{path.suffix}")


def _is_external(url: str) -> bool:
    parts = urlsplit(url)
    return bool(parts.scheme or parts.netloc) or url.startswith("//")


def _page_target(url: str) -> PurePosixPath:
    """The file a root-relative page URL is served from (`/en/` is `en/index.html`)."""
    path = unquote(urlsplit(url).path).lstrip("/")
    if path == "" or path.endswith("/"):
        path += "index.html"
    return PurePosixPath(path)


def _read_source(source: Path) -> dict[PurePosixPath, bytes]:
    if not source.is_dir():
        raise BuildError(f"site folder not found: {source}")
    files = {
        PurePosixPath(path.relative_to(source).as_posix()): path.read_bytes()
        for path in sorted(source.rglob("*"))
        if path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(source).parts)
    }
    if not any(path.suffix == ".html" for path in files):
        raise BuildError(f"no pages (*.html) in {source}")
    misplaced = [str(p) for p in files if p.suffix in HASHED_SUFFIXES and p.parts[0] != "assets"]
    if misplaced:
        # Outside assets/ these would keep a fixed name yet be cached for a year.
        raise BuildError(f"move these under assets/ so they get hashed names: {misplaced}")
    return files


def _rewrite_css(
    css_path: PurePosixPath,
    text: str,
    files: dict[PurePosixPath, bytes],
    renamed: dict[str, str],
) -> str:
    css_dir = css_path.parent.as_posix()

    def replace(match: re.Match[str]) -> str:
        url = match.group(2)
        if _is_external(url) or url.startswith("data:"):
            raise BuildError(f"{css_path}: only files in the site may be referenced, not {url}")
        joined = url.lstrip("/") if url.startswith("/") else posixpath.join(css_dir, url)
        target = PurePosixPath(posixpath.normpath(joined))
        if target not in files:
            raise BuildError(f"{css_path}: missing reference {url}")
        new_target = renamed.get(f"/{target}")
        if new_target is None:
            return match.group(0)
        new_url = posixpath.relpath(new_target.lstrip("/"), css_dir or ".")
        return f'url("{new_url}")'

    return CSS_URL.sub(replace, text)


def _rewrite_page(
    page_path: PurePosixPath,
    text: str,
    files: dict[PurePosixPath, bytes],
    renamed: dict[str, str],
) -> str:
    page = parse_page(text)
    for element, name, url in page.url_references():
        if url.startswith("mailto:"):
            continue
        if url.startswith("#"):
            if page.by_id(url[1:]) is None:
                raise BuildError(f"{page_path}: missing in-page target {url}")
            continue
        if _is_external(url) or not url.startswith("/"):
            raise BuildError(f"{page_path}: <{element.tag} {name}> must be root-relative: {url}")
        if _page_target(url) not in files:
            raise BuildError(f"{page_path}: missing reference {url}")
        new_url = renamed.get(urlsplit(url).path)
        if new_url is None:
            continue
        old_attr, new_attr = f'{name}="{url}"', f'{name}="{new_url}"'
        if old_attr not in text:
            raise BuildError(f"{page_path}: cannot rewrite {old_attr}; use double quotes")
        text = text.replace(old_attr, new_attr)
    return text


def _plan_output(
    files: dict[PurePosixPath, bytes],
) -> tuple[dict[PurePosixPath, bytes], dict[str, str]]:
    """Work out every output file and the asset renames, without touching the disk."""
    output: dict[PurePosixPath, bytes] = {}
    renamed: dict[str, str] = {}

    def hashable(path: PurePosixPath) -> bool:
        return path.parts[0] == "assets" and path.suffix in HASHED_SUFFIXES

    # Fonts and images first, so the stylesheet can point at their hashed names.
    for path, data in files.items():
        if hashable(path) and path.suffix != ".css":
            new_path = hashed_path(path, data)
            renamed[f"/{path}"] = f"/{new_path}"
            output[new_path] = data
    for path, data in files.items():
        if hashable(path) and path.suffix == ".css":
            css = _rewrite_css(path, data.decode("utf-8"), files, renamed).encode("utf-8")
            new_path = hashed_path(path, css)
            renamed[f"/{path}"] = f"/{new_path}"
            output[new_path] = css
    for path, data in files.items():
        if path.suffix == ".html":
            page = _rewrite_page(path, data.decode("utf-8"), files, renamed)
            output[path] = page.encode("utf-8")
        elif not hashable(path):
            output[path] = data
    return output, renamed


def _prepare_out_dir(source: Path, out: Path) -> None:
    if out == source or out in source.parents or source in out.parents:
        raise BuildError(f"output {out} must be outside the site folder {source}")
    if out.exists():
        if not out.is_dir():
            raise BuildError(f"output {out} exists and is not a folder")
        if any(out.iterdir()) and not (out / BUILD_MARKER).is_file():
            raise BuildError(f"refusing to replace {out}: it was not made by this build")
        shutil.rmtree(out)
    out.mkdir(parents=True)


def build_site(source: Path, out: Path) -> BuildResult:
    """Build `source` into `out`, replacing an earlier build there."""
    source, out = source.resolve(), out.resolve()
    files = _read_source(source)
    output, renamed = _plan_output(files)
    _prepare_out_dir(source, out)
    for path, data in sorted(output.items()):
        target = out / Path(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (out / BUILD_MARKER).write_text("Built by scripts/build_site.py; safe to delete.\n")
    return BuildResult(
        out_dir=out,
        renamed=dict(sorted(renamed.items())),
        pages=tuple(sorted(str(p) for p in output if p.suffix == ".html")),
        files=tuple(sorted(str(p) for p in output)),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=REPO_ROOT / "site")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "dist")
    args = parser.parse_args(argv)
    try:
        result = build_site(args.source, args.out)
    except BuildError as error:
        print(f"build failed: {error}", file=sys.stderr)
        return 1
    print(f"Built {len(result.files)} files into {result.out_dir}")
    for old, new in result.renamed.items():
        print(f"  {old} -> {new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
