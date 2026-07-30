"""`skrepka logout` / `skrepka revoke` / `skrepka forget` — sign-out, server-side
revocation, and local data erasure. Routed from cli.py like init/doctor.

Storage in 0.9 is a 0600 token file in the config dir (OS-keyring/AEAD encryption
is deferred post-0.9), so these commands operate on that file plus the other
local artifacts. All three serialize with the config lock so they cannot
interleave with a concurrent init/reauth/refresh.

Safety:
  * The refresh token is sent ONLY as a POST form body to Google's revocation
    endpoint — never in a URL, a log line, or an exception message. Redirects
    are disabled so a 3xx cannot carry the token to another origin.
  * `revoke` and `forget` are destructive; run non-interactively they REFUSE
    unless `--yes` is passed, so a stray non-interactive call cannot wipe data
    by accident. `--yes` is a deliberate human action; agents are contractually
    forbidden from invoking these (see agents/CONTRACT.md). This is a
    cooperative boundary, not a mechanical block on a determined caller.
  * `revoke` treats only an unambiguous success (HTTP 200, or 400 invalid_token
    = already dead) as revoked; any ambiguous outcome KEEPS the local token.
  * Exactly one JSON object goes to stdout; narration goes to stderr.
"""

import json
import os
import re
import sys

from skrepka import config, setup

REVOKE_URI = "https://oauth2.googleapis.com/revoke"

# atomic-write crash leftover shape: `.<name>.<16 hex>.tmp` (config.py writes
# `.{name}.{secrets.token_hex(8)}.tmp`; token_hex(8) is 16 hex chars).
_CRASH_TEMP_RE = re.compile(r"\..+\.[0-9a-f]{16}\.tmp")


def _emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# --- artifact enumeration -------------------------------------------------

def _dir_files():
    try:
        return sorted(os.listdir(config.config_dir()))
    except (FileNotFoundError, config.ConfigError):
        return []


def _smoke_journals(files):
    return [f for f in files
            if f.startswith(config.JOURNAL_PREFIX) and f.endswith(".json")
            and not f.startswith(setup.TOKEN_PREFIX)]


def _smoke_tokens(files):
    return [f for f in files
            if f.startswith(setup.TOKEN_PREFIX) and f.endswith(".json")]


def _token_crash_temps(files):
    """Crash temps whose base name is a TOKEN file.

    `_crash_temps` matches every atomic-write leftover, including temps of
    `credentials.json` and of the recovery client. `logout` promises to keep
    the OAuth client, so it must not sweep those — only the ones that can hold
    a refresh token."""
    bases = (config.TOKEN_NAME, setup.RECOVERY_TOKEN)
    return [f for f in _crash_temps(files)
            if f.startswith(tuple(f".{b}." for b in bases))
            or f.startswith(f".{setup.TOKEN_PREFIX}")]


def _token_bearing(files):
    """Every on-disk artifact that can still authenticate as you (no marker).

    logout, the cleanup after revoke, and forget must agree on this list;
    keeping three hand-written copies is how `recovery.token.json` and the
    crash temps ended up in forget only. After an interrupted reauth those hold
    a working refresh token, so leaving them behind broke logout's promise that
    the agent can no longer reach Google."""
    return ([config.TOKEN_NAME, setup.RECOVERY_TOKEN]
            + _smoke_tokens(files) + _token_crash_temps(files))


def _usable_token_targets(files):
    """Token-bearing artifacts, then the write-ahead marker LAST.

    The marker is a barrier that makes readers fail closed, so if a delete
    fails partway through, the marker must still be standing rather than
    exposing a half-dismantled sign-in."""
    return _token_bearing(files) + [config.MARKER_NAME]


def _exists(name):
    return os.path.lexists(os.path.join(config.config_dir(), name))


def _remove_existing(names):
    """Remove each config-dir artifact that exists; return the list actually
    removed and a list of (name) that failed."""
    removed, failed = [], []
    for name in names:
        if not _exists(name):
            continue
        try:
            config.remove_secret(name)
            removed.append(name)
        except OSError:
            failed.append(name)
    return removed, failed


def _crash_temps(files):
    """Atomic-write crash leftovers `.<name>.<16 hex>.tmp` in the config dir —
    these can hold a half-written secret after a SIGKILL and must be swept by
    forget (codex r3-priv #P2). Matched by exact shape so unrelated dotfiles
    (e.g. `.foo.tmp`) are NOT deleted; `.lock` has no `.tmp` suffix."""
    return [f for f in files if _CRASH_TEMP_RE.fullmatch(f)]


def _cleanup_after_revoke(revoked_hash):
    """Under the lock, remove the local token ONLY if it is PROVABLY still the
    one we just revoked. A concurrent `init --reauth` may have installed a
    different token while the network revoke was in flight — never delete that
    newer sign-in (codex r3-priv #P1). If the store cannot be re-read, fail
    closed and keep the token. Returns a dict with keys: superseded, verifiable,
    removed, failed, token_present (re-checked AFTER cleanup — codex r3-priv
    #P2)."""
    with config.lock():
        try:
            env = config.read_token_envelope()
        except config.ConfigError:
            # cannot prove which token is on disk → never delete blindly
            return {"superseded": False, "verifiable": False,
                    "removed": [], "failed": [config.TOKEN_NAME],
                    "token_present": True}
        current_hash = config.refresh_token_hash((env or {}).get("token") or {})
        if env is not None and current_hash != revoked_hash:
            return {"superseded": True, "verifiable": True,
                    "removed": [], "failed": [], "token_present": True}
        files = _dir_files()
        targets = _usable_token_targets(files)
        removed, failed = _remove_existing(targets)
        return {"superseded": False, "verifiable": True,
                "removed": removed, "failed": failed,
                "token_present": _exists(config.TOKEN_NAME)}


def _confirm(prompt, assume_yes):
    """Return True (confirmed), False (declined), or None (cannot confirm — no
    interactive terminal and --yes not given)."""
    if assume_yes:
        return True
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return None
    sys.stderr.write(prompt + " [y/N]: ")
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() in ("y", "yes")


# --- logout ---------------------------------------------------------------

def cmd_logout(argv):
    setup._SafeArgParser(prog="skrepka logout").parse_args(argv)
    with config.lock():
        files = _dir_files()
        # every USABLE token, marker last (see _usable_token_targets)
        targets = _usable_token_targets(files)
        removed, failed = _remove_existing(targets)
    _emit({
        "action": "logout",
        "status": "signed_out" if not failed else "signed_out_incomplete",
        "removed": removed,
        "failed": failed,
        "kept": [config.CREDENTIALS_NAME],
        "note": ("local sign-in removed; access at Google was NOT revoked — "
                 "run `skrepka revoke` to revoke server-side. Re-connect with "
                 "`skrepka init`."),
    })
    return 1 if failed else 0


# --- revoke ---------------------------------------------------------------

def _parse_revoke_error(resp):
    """Extract only Google's `error` slug from a 400 body — never the body
    itself (defensive; the token is not echoed but we stay strict)."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if isinstance(body, dict):
        err = body.get("error")
        return err if isinstance(err, str) else None
    return None


def cmd_revoke(argv):
    p = setup._SafeArgParser(prog="skrepka revoke")
    p.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)

    try:
        env = config.read_token_envelope()
    except config.ConfigError as e:
        _emit({"action": "revoke", "status": "error",
               "error": setup._safe_code(e)})
        return 1
    refresh_token = ((env or {}).get("token") or {}).get("refresh_token")
    if not refresh_token:
        # There is nothing to revoke server-side, but an interrupted reauth can
        # leave a recovery/smoke token behind that still authenticates. Revoke
        # must not imply those are gone — name them and point at logout.
        leftovers = [n for n in _token_bearing(_dir_files())
                     if n != config.TOKEN_NAME and _exists(n)]
        payload = {"action": "revoke", "status": "nothing_to_revoke",
                   "note": "no stored sign-in with a refresh token to revoke"}
        if leftovers:
            payload["local_tokens_still_present"] = leftovers
            payload["note"] += (
                "; other local token files remain and may still work — "
                "run `skrepka logout` (or `forget`) to remove them")
        _emit(payload)
        return 0

    confirmed = _confirm(
        "Revoking tells Google to invalidate this grant. It can also "
        "invalidate tokens of OTHER apps under the same Cloud project and "
        "remove the whole scope grant. Continue?", args.yes)
    if confirmed is None:
        _emit({"action": "revoke", "status": "needs_confirmation",
               "error": "revoke is destructive and needs an interactive "
                        "terminal; pass --yes to confirm non-interactively"})
        return 2
    if not confirmed:
        _emit({"action": "revoke", "status": "cancelled"})
        return 0

    try:
        import requests
    except ImportError:
        _emit({"action": "revoke", "status": "error", "error": "not_installed"})
        return 1

    # POST the token as a form body only; never in the URL/logs. No redirects.
    try:
        resp = requests.post(
            REVOKE_URI, data={"token": refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30, allow_redirects=False)
    except requests.exceptions.RequestException:
        _emit({"action": "revoke", "status": "ambiguous",
               "revoked": False, "token_kept": True,
               "note": "could not reach Google's revocation endpoint (network "
                       "error/timeout) — local sign-in kept; retry later"})
        return 1

    status = resp.status_code
    if status == 200:
        outcome = "revoked"
    elif status == 400 and _parse_revoke_error(resp) == "invalid_token":
        outcome = "revoked"  # already invalid server-side = success
    elif status == 400 and _parse_revoke_error(resp) == "invalid_request":
        outcome = "request_error"  # deterministic bad request, not ambiguous
    else:
        outcome = "ambiguous"  # 3xx/429/5xx/other 4xx → keep token

    if outcome == "revoked":
        revoked_hash = config.refresh_token_hash(
            {"refresh_token": refresh_token})
        r = _cleanup_after_revoke(revoked_hash)
        if r["superseded"]:
            _emit({"action": "revoke", "status": "revoked_superseded_locally",
                   "revoked": True, "token_kept": True,
                   "removed": r["removed"], "failed": r["failed"],
                   "note": "access for the revoked sign-in was revoked at "
                           "Google, but a NEWER local sign-in replaced it and "
                           "was kept — run `skrepka revoke` again to revoke "
                           "that one too"})
            return 0
        if not r["verifiable"]:
            _emit({"action": "revoke",
                   "status": "revoked_local_cleanup_failed", "revoked": True,
                   "token_kept": True, "removed": r["removed"],
                   "failed": r["failed"],
                   "note": "access revoked at Google, but the local token store "
                           "could not be read to clean up — run `skrepka forget`"})
            return 1
        cleanup_ok = not r["failed"]
        _emit({
            "action": "revoke",
            "status": "revoked" if cleanup_ok else "revoked_local_cleanup_failed",
            "revoked": True, "token_kept": r["token_present"],
            "removed": r["removed"], "failed": r["failed"],
            "note": ("access revoked at Google and local sign-in removed"
                     if cleanup_ok else
                     "access revoked at Google, but some local files could not "
                     "be removed — run `skrepka forget` to finish cleanup")})
        return 0 if cleanup_ok else 1
    if outcome == "request_error":
        _emit({"action": "revoke", "status": "request_error",
               "revoked": False, "token_kept": True,
               "note": "Google rejected the revocation request; local sign-in "
                       "kept"})
        return 1
    _emit({"action": "revoke", "status": "ambiguous", "revoked": False,
           "token_kept": True,
           "note": "Google did not confirm revocation (unexpected response) — "
                   "local sign-in kept; retry later"})
    return 1


# --- forget ---------------------------------------------------------------

def _sidecar_targets(md_paths):
    """For each user --sidecars md path, the skrepka-written sidecar sitting
    next to it (an explicit path — never a global filesystem search)."""
    out = []
    for md in md_paths:
        cand = md + config.SIDECAR_SUFFIX
        if os.path.lexists(cand):
            out.append(cand)
    return out


def _forget_targets():
    """Every config-dir artifact forget removes: known secret files, smoke
    journals/tokens, recovery files, and atomic-write crash temps. `.lock` and
    the config dir itself are deliberately NOT included."""
    files = _dir_files()
    # same token list as logout/revoke (single source), plus the client side,
    # the journals and any remaining crash temps; marker last as everywhere
    known = (_token_bearing(files)
             + [config.CREDENTIALS_NAME, setup.RECOVERY_CRED]
             + _smoke_journals(files) + _crash_temps(files)
             + [config.MARKER_NAME])
    out, seen = [], set()
    for name in known:
        if name in seen:
            continue
        seen.add(name)
        if _exists(name):
            out.append(name)
    return out


def cmd_forget(argv):
    p = setup._SafeArgParser(prog="skrepka forget")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--sidecars", action="append", default=[], metavar="MD_PATH")
    args = p.parse_args(argv)

    sidecars = _sidecar_targets(args.sidecars)
    external = ("skrepka cannot remove copies already at Google Drive, at your "
                "AI provider, in git history, or in backups/snapshots; local "
                "unlink is logical deletion, not physical erasure on SSDs")
    kept = [".lock (in-use lock file)", "the config directory itself"]

    if args.dry_run:
        _emit({"action": "forget", "dry_run": True,
               "would_remove": _forget_targets() + sidecars, "kept": kept,
               "external_copies_not_controlled": external})
        return 0

    confirmed = _confirm(
        f"This permanently deletes {len(_forget_targets()) + len(sidecars)} "
        f"local skrepka file(s) including your sign-in. Continue?", args.yes)
    if confirmed is None:
        _emit({"action": "forget", "status": "needs_confirmation",
               "error": "forget is destructive and needs an interactive "
                        "terminal; pass --yes (or use --dry-run first)"})
        return 2
    if not confirmed:
        _emit({"action": "forget", "status": "cancelled"})
        return 0

    with config.lock():
        # re-enumerate INSIDE the lock: an artifact created between the pre-lock
        # count and now must not be missed (codex r3-priv #P2)
        removed, failed = _remove_existing(_forget_targets())
    for sc in sidecars:
        try:
            os.remove(sc)  # removing a symlink drops the link, not its target
            removed.append(sc)
        except OSError:
            failed.append(sc)

    _emit({"action": "forget", "dry_run": False,
           "local_data_removed": removed, "failed": failed, "kept": kept,
           "external_copies_not_controlled": external})
    return 1 if failed else 0


# --- entry points (called from cli.py) ------------------------------------

def logout_main(argv):
    return _guard(cmd_logout, argv, "logout")


def revoke_main(argv):
    return _guard(cmd_revoke, argv, "revoke")


def forget_main(argv):
    return _guard(cmd_forget, argv, "forget")


def _guard(fn, argv, action):
    try:
        return fn(argv)
    except setup._ArgError:
        print(f"Invalid usage. See `skrepka {action} --help`.", file=sys.stderr)
        return 2
    except SystemExit:
        raise  # argparse --help already printed
    except config.ConfigError as e:
        _emit({"action": action, "status": "error", "error": setup._safe_code(e)})
        return 1
    except BaseException as e:  # noqa: BLE001
        _emit({"action": action, "status": "error", "error": setup._safe_code(e)})
        return 1
