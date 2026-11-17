"""Shared fixtures for the RaadWeb test suite."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = REPO_ROOT / "site"


@pytest.fixture(scope="session")
def site_dir() -> Path:
    """The hand-written site source (`site/`)."""
    return SITE_DIR
