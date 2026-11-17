"""Checks that the GitHub workflows pin their tools.

Every third-party action must be used by a full 40-character commit SHA with its release in a
comment (`uses: owner/repo@<sha> # v1.2.3`), never by a moving tag or branch. The AWS CDK CLI must
be installed at an exact version whose major matches `aws-cdk-lib` in `uv.lock`, so the CLI and the
library's cloud-assembly schema cannot drift apart.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<value>\S+)(?P<rest>.*)$")
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")
RELEASE_COMMENT = re.compile(r"^\s*#\s*v\d+\.\d+\.\d+\s*$")
CDK_CLI = re.compile(r"\baws-cdk(?:@(?P<version>\S+))?(?=\s|$)")
EXACT_VERSION = re.compile(r"^(?P<major>\d+)\.\d+\.\d+$")


def unpinned_actions(text: str) -> list[str]:
    """Every `uses:` line that is not a full commit SHA with a release comment."""
    problems = []
    for line in text.splitlines():
        match = USES.match(line)
        if not match:
            continue
        value, rest = match["value"].strip("'\""), match["rest"]
        if value.startswith("./"):
            continue  # an action in this repository
        if not PINNED_ACTION.match(value) or not RELEASE_COMMENT.match(rest):
            problems.append(line.strip())
    return problems


def cdk_cli_versions(text: str) -> list[str | None]:
    """The version of every `aws-cdk` CLI install or run (`None` when no version is given)."""
    versions: list[str | None] = []
    for line in text.splitlines():
        if not re.search(r"\b(npm|npx|pnpm|yarn)\b", line):
            continue
        versions.extend(match["version"] for match in CDK_CLI.finditer(line))
    return versions


def cdk_cli_problems(text: str, library_major: str) -> list[str]:
    problems = []
    for version in cdk_cli_versions(text):
        exact = EXACT_VERSION.match(version or "")
        if not exact:
            problems.append(f"aws-cdk is not pinned to an exact version: {version!r}")
        elif exact["major"] != library_major:
            problems.append(f"aws-cdk {version} does not match aws-cdk-lib major {library_major}")
    return problems


def locked_cdk_lib_major() -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    [version] = [p["version"] for p in lock["package"] if p["name"] == "aws-cdk-lib"]
    return str(version).split(".")[0]


def test_the_repository_has_its_two_workflows() -> None:
    assert [w.name for w in WORKFLOWS] == ["ci.yml", "deploy-dev.yml"]


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda w: w.name)
def test_every_action_is_pinned_to_a_commit_sha(workflow: Path) -> None:
    text = workflow.read_text(encoding="utf-8")
    assert any(USES.match(line) for line in text.splitlines()), "the workflow uses actions"
    assert unpinned_actions(text) == []


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda w: w.name)
def test_cdk_cli_is_pinned_to_the_locked_library_major(workflow: Path) -> None:
    text = workflow.read_text(encoding="utf-8")
    assert cdk_cli_versions(text), "the workflow installs the CDK CLI"
    assert cdk_cli_problems(text, locked_cdk_lib_major()) == []


def test_both_workflows_use_the_same_cdk_cli_version() -> None:
    versions = {v for w in WORKFLOWS for v in cdk_cli_versions(w.read_text(encoding="utf-8"))}
    assert len(versions) == 1, versions


@pytest.mark.parametrize(
    "line",
    [
        "      - uses: actions/checkout@v4",
        "      - uses: astral-sh/setup-uv@main",
        "      - uses: gitleaks/gitleaks-action@ff98106e4c7b2bc287b24eaf42907196329070c7",
        "      - uses: actions/setup-node@49933ea # v4.4.0",
        "        uses: aws-actions/configure-aws-credentials@v4.3.1 # v4.3.1",
    ],
)
def test_checker_flags_tags_branches_short_shas_and_missing_release_comments(line: str) -> None:
    assert unpinned_actions(line) == [line.strip()]


def test_checker_accepts_a_pinned_action_and_local_actions() -> None:
    pinned = "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0"
    assert unpinned_actions(pinned + "\n      - uses: ./.github/actions/setup") == []


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ("      - run: npm install --global aws-cdk", "not pinned to an exact version: None"),
        ("      - run: npm install -g aws-cdk@latest", "not pinned to an exact version"),
        ("      - run: npm install -g aws-cdk@^2.1148.1", "not pinned to an exact version"),
        ("      - run: npx aws-cdk@3.0.0 synth", "does not match aws-cdk-lib major 2"),
    ],
)
def test_checker_flags_an_unpinned_or_mismatched_cdk_cli(line: str, problem: str) -> None:
    [found] = cdk_cli_problems(line, "2")
    assert problem in found
    assert cdk_cli_problems("      - run: npm install --global aws-cdk@2.1148.1", "2") == []
