"""Console entry point.

Routes `init` / `doctor` to the setup module (repo-only; the reviewed engine
is unchanged) and everything else to the engine. The engine imports the Google
client libraries at module load, so we defer that import and wrap it: a missing
dependency becomes a friendly message instead of a raw traceback (plan r1 #16).
For `doctor --json` the safe-output boundary is established BEFORE the engine
import so a dependency/import failure still yields one sanitized JSON (r3 #4).
"""

import json
import sys

_INSTALL_HINT = (
    "skrepka is not fully installed (missing dependencies) — reinstall with "
    "`pipx install skrepka` or `uv tool install skrepka`")

# Curated top-level help. The engine and the setup/privacy modules each own
# their own argparse, so no single parser lists every command; without this,
# `skrepka --help` would hide `init` — the first command a new user needs.
_TOP_HELP = """skrepka — careful collaborative editing for Google Docs.

Setup:
  init         Guided Google authorization (run this first)
  doctor       Diagnose credentials, token, scopes, and API access

Comments & edits:
  comments     List comments on a doc
  reply        Post a reply to a comment (--file for a batch,
               each reply in its own second)
  resolve      Resolve a comment thread (the person's decision, not an agent's)
  comment      Create a document-level comment
  patch        Apply anchor-safe text edits (keeps comment threads alive —
               start here when a doc has comments)
  mark         Create a named range around a text fragment
  suggestions  List suggestions on a doc

Documents:
  upload       Create a Google Doc from a .md file
  download     Export a Google Doc as markdown
  update       Replace a doc's whole content — DESTROYS every comment thread
  upload-file  Upload file(s) as-is (no Google Doc conversion)
  sync         Three-way merge a local .md into a doc, keeping OPEN comment
               threads (experimental; refuses when the edit rewrites
               commented text; a closed thread can be unhooked — its words
               are archived next to the .md first)

Data & privacy:
  logout       Remove the local token (keeps your OAuth client)
  revoke       Revoke the token with Google, then remove it locally
  forget       Remove the local token/credentials/journals (and, with
               --sidecars PATH, a document's sidecar)

Other:
  --version    Print the installed version

Run `skrepka <command> --help` for details on a command.
"""


def _bootstrap_error(msg):
    sys.stdout.write(json.dumps({"error": msg}) + "\n")
    sys.exit(1)


def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else None

    # Bare `skrepka`, or a lone help request, prints the curated overview and
    # exits 0. `help <cmd>` / `-h <extra>` are NOT swallowed here: they fall
    # through so an unknown top-level token still fails loudly.
    if not argv or argv in (["-h"], ["--help"], ["help"]):
        sys.stdout.write(_TOP_HELP)
        sys.exit(0)

    # Bug reports need a version to quote (#5). Compared as the whole argv, so
    # `skrepka --version extra` still fails loudly instead of exiting 0; the
    # version lives in skrepka/__init__.py, which imports nothing heavy, so a
    # broken dependency install can still answer this.
    if argv == ["--version"]:
        from skrepka import __version__
        sys.stdout.write(f"skrepka {__version__}\n")
        sys.exit(0)

    if cmd == "init":
        try:
            from skrepka import setup
        except ImportError:
            _bootstrap_error(_INSTALL_HINT)
        sys.exit(setup.init_main(argv[1:]))

    if cmd == "doctor":
        json_mode = "--json" in argv[1:]
        try:
            from skrepka import setup
        except ImportError:
            if json_mode:
                sys.stdout.write(json.dumps(
                    {"action": "doctor", "ok": False,
                     "error": "not_installed"}) + "\n")
                sys.exit(2)
            _bootstrap_error(_INSTALL_HINT)
        sys.exit(setup.doctor_main(argv[1:]))

    if cmd in ("logout", "revoke", "forget"):
        try:
            from skrepka import privacy
        except ImportError:
            _bootstrap_error(_INSTALL_HINT)
        entry = {"logout": privacy.logout_main,
                 "revoke": privacy.revoke_main,
                 "forget": privacy.forget_main}[cmd]
        sys.exit(entry(argv[1:]))

    try:
        from skrepka._engine import main as engine_main
    except ImportError:
        _bootstrap_error(_INSTALL_HINT)
    engine_main()


if __name__ == "__main__":
    main()
