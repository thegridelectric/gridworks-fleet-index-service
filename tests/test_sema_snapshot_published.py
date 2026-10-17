"""The vendored sema snapshot ships only published vocabulary on `main`.

A snapshot built with `--allow-staged` is for dev brokers only (sema spec,
snapshot "Published-only by default"); the fleet index service deploys
`main`.
The snapshot's own `definitions/registry.yaml` records the status of every
word and version it ships, so the test reads that and nothing outside the
repo. Off `main` the non-published words are reported, not failed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "src" / "fis" / "sema"
PRODUCTION_BRANCH = "main"


def current_branch() -> str:
    """The branch under test: GitHub Actions' ref (the PR's target on a
    pull request), else the checkout's."""
    for key in ("GITHUB_BASE_REF", "GITHUB_REF_NAME"):
        if os.environ.get(key):
            return os.environ[key]
    out = subprocess.run(
        ["git", "-C", str(ROOT), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def non_published(snapshot: Path) -> list[str]:
    """Every word/version in the snapshot registry whose status is not
    published. A versionless word (a format, `gw`) has its status at the
    top of its entry; every other word has one per version."""
    registry = yaml.safe_load((snapshot / "definitions" / "registry.yaml").read_text())
    found: list[str] = []
    for kind in ("formats", "enums", "types"):
        for name, entry in (registry.get(kind) or {}).items():
            versions = entry.get("versions")
            if versions is None:
                if entry.get("status") != "published":
                    found.append(f"{kind[:-1]} {name} ({entry.get('status')})")
                continue
            for version, version_entry in versions.items():
                if version_entry.get("status") != "published":
                    found.append(
                        f"{kind[:-1]} {name}:{version} ({version_entry.get('status')})"
                    )
    return sorted(found)


def test_snapshot_is_published_only_on_main() -> None:
    offenders = non_published(SNAPSHOT)
    if not offenders:
        return
    report = "src/fis/sema holds non-published words:\n  " + "\n  ".join(offenders)
    branch = current_branch()
    if branch == PRODUCTION_BRANCH:
        pytest.fail(report)
    pytest.skip(f"on {branch!r}, not {PRODUCTION_BRANCH!r}; {report}")
