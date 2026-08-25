"""Offline contract for the public build and release supply chain."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = [
    ROOT / ".github" / "workflows" / "ci.yml",
    ROOT / ".github" / "workflows" / "release.yml",
]
GITLEAKS_SHA256 = (
    "79a3ab579b53f71efd634f3aaf7e04a0fa0cf206b7ed434638d1547a2470a66e"
)


def test_every_external_action_is_pinned_to_a_commit_with_version_note():
    uses = []
    for path in WORKFLOWS:
        uses.extend(
            line.strip() for line in path.read_text().splitlines()
            if line.strip().startswith("- uses:")
        )

    assert uses
    assert all(re.fullmatch(
        r"- uses: [A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40} # v\d[^ ]*",
        line,
    ) for line in uses), uses


def test_gitleaks_archive_is_verified_before_extraction():
    for path in WORKFLOWS:
        text = path.read_text()
        if "gitleaks_8.30.0_linux_x64.tar.gz" not in text:
            continue
        assert GITLEAKS_SHA256 in text
        assert "sha256sum -c -" in text
        assert text.index("sha256sum -c -") < text.index(
            "tar -xzf gitleaks.tar.gz")
        assert "| tar" not in text


def test_sdist_does_not_ship_an_image_less_quickstart():
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'exclude = ["/docs/img", "/docs/QUICKSTART.md"]' in pyproject

    for path in WORKFLOWS:
        text = path.read_text()
        if "assert sdist excludes private paths" in text:
            assert 'm.name.endswith("/docs/QUICKSTART.md")' in text
            assert '"/docs/img/"' in text
