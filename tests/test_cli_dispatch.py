"""Top-level CLI dispatch (cli.py). The curated overview must be reachable and
must list the setup/privacy commands that have no entry in any single argparse
parser (regression: `init` was invisible to `--help`). The `help` keyword with
an extra token (e.g. `help patch`) is NOT swallowed as success — it falls
through and fails loudly. (`-h`/`--help` follow the usual argparse convention:
they print help and exit 0 regardless of any trailing token.)"""

import sys

import pytest

from skrepka import cli

_ALL_COMMANDS = ("init", "doctor", "comments", "reply", "patch",
                 "upload", "download", "logout", "revoke", "forget")


def _run(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["skrepka"] + argv)
    with pytest.raises(SystemExit) as ei:
        cli.main()
    return ei.value.code


def test_bare_invocation_prints_overview_and_exits_0(monkeypatch, capsys):
    code = _run([], monkeypatch)
    out = capsys.readouterr().out
    assert code == 0
    for c in _ALL_COMMANDS:
        assert c in out, f"{c} missing from top-level help"


@pytest.mark.parametrize("arg", ["-h", "--help", "help"])
def test_lone_help_request_exits_0(monkeypatch, capsys, arg):
    code = _run([arg], monkeypatch)
    assert code == 0
    assert "init" in capsys.readouterr().out


def test_help_with_extra_token_does_not_silently_succeed(monkeypatch):
    # `skrepka help patch` must not print the overview and return 0; it falls
    # through to the engine parser, which rejects the unknown token.
    code = _run(["help", "patch"], monkeypatch)
    assert code != 0


def test_version_prints_the_installed_version(monkeypatch, capsys):
    """Bug reports have to quote a version, and CONTRIBUTING points here (#5).
    It must answer before the engine (and its google deps) is imported, so a
    half-broken install can still be identified."""
    import skrepka

    monkeypatch.setitem(sys.modules, "skrepka._engine", None)
    code = _run(["--version"], monkeypatch)
    out = capsys.readouterr().out
    assert code == 0
    assert out.strip() == f"skrepka {skrepka.__version__}"
    assert "--version" in cli._TOP_HELP


def test_version_with_extra_token_does_not_silently_succeed(monkeypatch):
    # same rule as `help patch`: an unknown trailing token must fail loudly
    code = _run(["--version", "extra"], monkeypatch)
    assert code != 0
