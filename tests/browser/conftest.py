"""Fixtures for the browser scan: a local build served with the production headers, and Chromium.

If Chromium cannot be started on this machine the tests are SKIPPED with the reason, which is
"not run", never "passed". CI sets RAADWEB_REQUIRE_BROWSER=1 so a missing browser fails there.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Error, sync_playwright

from scripts.build_site import build_site
from tests.site_server import serve


@pytest.fixture(scope="session")
def built_site(tmp_path_factory: pytest.TempPathFactory, site_dir: Path) -> Path:
    return build_site(site_dir, tmp_path_factory.mktemp("browser") / "dist").out_dir


@pytest.fixture(scope="session")
def base_url(built_site: Path) -> Iterator[str]:
    with serve(built_site) as url:
        yield url


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            chromium = playwright.chromium.launch()
        except Error as error:
            reason = f"NOT RUN: headless Chromium could not start ({error.message.splitlines()[0]})"
            if os.environ.get("RAADWEB_REQUIRE_BROWSER") == "1":
                pytest.fail(reason)
            pytest.skip(reason)
        yield chromium
        chromium.close()
