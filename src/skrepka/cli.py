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


def _bootstrap_error(msg):
    sys.stdout.write(json.dumps({"error": msg}) + "\n")
    sys.exit(1)


def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else None

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
