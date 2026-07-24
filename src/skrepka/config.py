"""Path resolution + hardened secret I/O + config transactions.

Threat model (docs/THREAT-MODEL, R2): a SAME-UID local attacker is OUT of
scope — they can read the token directly. We defend against a malicious
credentials.json, a shared/foreign-owned or group/world-writable config
parent, symlink tricks on secret files, leakage into diagnostics, and a
crash mid-`init`/`--reauth` leaving a half-activated credentials/token set.

R3 will swap the file backend for an OS keyring and add path migration
behind this same interface; keep callers using the accessors here.
"""

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat

DEFAULT_DIRNAME = "skrepka"
LEGACY_DIRNAME = "gdocs-uploader"

CREDENTIALS_NAME = "credentials.json"
TOKEN_NAME = "token.json"          # holds the envelope (token + provenance)
MARKER_NAME = "commit.pending"     # write-ahead marker; barrier for readers
JOURNAL_PREFIX = "smoke-"          # per-nonce cleanup journals

# Suffix of the merge-base sidecar written next to a downloaded markdown file
# (single source of truth, shared by the engine and the `forget` command).
SIDECAR_SUFFIX = ".gdocs-base.json"

_MAX_SECRET_BYTES = 256 * 1024     # credentials/token are a few KB; cap hard


class ConfigError(Exception):
    """Configuration or secret-store integrity failure (fail closed)."""


class PendingInitError(ConfigError):
    """A write-ahead commit marker is present: init/reauth did not finish.

    Normal commands must fail closed on this; only `init` may resolve it.
    """


def _home():
    home = os.environ.get("HOME")
    if not home:
        home = os.path.expanduser("~")
    if not home or home == "~":
        raise ConfigError("cannot resolve HOME — set HOME and retry")
    return os.path.realpath(home)


def config_dir():
    """Resolve the config directory (LITERAL path — not realpath, so the
    chain check can still see and refuse symlink components). An
    SKREPKA_CONFIG_DIR override must resolve inside the real HOME (r3 #5),
    otherwise we cannot vouch for the parent chain and refuse."""
    override = os.environ.get("SKREPKA_CONFIG_DIR")
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        home = _home()
        rp = os.path.realpath(path)
        if rp != home and not rp.startswith(home + os.sep):
            raise ConfigError(
                "SKREPKA_CONFIG_DIR must be inside your home directory "
                "(cannot verify the ownership of an outside location)")
        return path
    return os.path.join(os.path.expanduser("~/.config"), DEFAULT_DIRNAME)


def legacy_dir():
    return os.path.join(os.path.expanduser("~/.config"), LEGACY_DIRNAME)


def _path(name):
    return os.path.join(config_dir(), name)


def _check_component(path, uid):
    """One path component must be a real dir/file owned by uid and not
    group/world-writable, and not a symlink (r3 #5)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise ConfigError(f"{path} is a symlink — refusing (fail closed)")
    if st.st_uid != uid:
        raise ConfigError(
            f"{path} is not owned by you (uid {st.st_uid}) — refusing")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(
            f"{path} is group- or world-writable — refusing (fail closed)")


def _check_dir_chain(path):
    """Validate every LITERAL component from `path` up to HOME with lstat, so
    a symlink component is refused BEFORE it is resolved (r3 #5 / code-r1 #6).
    Runs on every secret access, not only from init/doctor."""
    uid = os.getuid()
    home = _home()
    cur = os.path.abspath(path)
    seen = set()
    while True:
        _check_component(cur, uid)
        if os.path.realpath(cur) == home:
            break
        parent = os.path.dirname(cur)
        if parent == cur or cur in seen:
            # reached filesystem root without hitting HOME: the config dir
            # is not under HOME — config_dir() should have refused already
            raise ConfigError(
                f"{path} is not located under your home directory")
        seen.add(cur)
        cur = parent


def ensure_config_dir():
    """Create the config dir 0700 if needed and validate the parent chain."""
    d = config_dir()
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
    except OSError as e:
        raise ConfigError(f"cannot create config dir: {e}")
    # tighten an existing dir that we own; refuse one we don't
    st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode):
        raise ConfigError(f"{d} is a symlink — refusing (fail closed)")
    if st.st_uid != os.getuid():
        raise ConfigError(f"{d} is not owned by you — refusing")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        os.chmod(d, 0o700)
    _check_dir_chain(d)
    return d


def _open_read_nofollow(path):
    """Open a secret for reading without following a final symlink; verify
    the opened object (via the same fd) is a regular file we own."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno in (errno.ENOENT,):
            return None
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise ConfigError(f"{path} is a symlink — refusing (fail closed)")
        raise ConfigError(f"cannot open {path}: {e}")
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise ConfigError(f"{path} is not a regular file — refusing")
    if st.st_uid != os.getuid():
        os.close(fd)
        raise ConfigError(f"{path} is not owned by you — refusing")
    if st.st_size > _MAX_SECRET_BYTES:
        os.close(fd)
        raise ConfigError(f"{path} is implausibly large — refusing")
    return fd


def read_secret_bytes(name):
    """Return the raw bytes of a secret file, or None if absent. Validates the
    parent chain on every access so normal commands are protected too, not
    only init/doctor (code-r1 #6)."""
    _check_dir_chain(config_dir())
    fd = _open_read_nofollow(_path(name))
    if fd is None:
        return None
    try:
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_secret_bytes(name, data):
    """Atomically write a secret 0600: unpredictable O_EXCL temp in the same
    dir, fsync file, os.replace, fsync dir. Refuses a symlinked target."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    d = config_dir()
    _check_dir_chain(d)
    target = os.path.join(d, name)
    # refuse to clobber via a symlinked target (O_TRUNC would follow it)
    try:
        st = os.lstat(target)
        if stat.S_ISLNK(st.st_mode):
            raise ConfigError(
                f"{target} is a symlink — refusing to overwrite (fail closed)")
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError(f"{target} is not a regular file — refusing")
        if st.st_uid != os.getuid():
            raise ConfigError(f"{target} is not owned by you — refusing")
    except FileNotFoundError:
        pass
    tmpname = os.path.join(d, f".{name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmpname, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        # write ALL bytes — a short write must not commit a truncated secret
        view = memoryview(data)
        written = 0
        while written < len(data):
            n = os.write(fd, view[written:])
            if n <= 0:
                raise ConfigError("short write while persisting a secret")
            written += n
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmpname)
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    try:
        os.replace(tmpname, target)
    except OSError as e:
        try:
            os.unlink(tmpname)  # do not leave a secret-bearing temp behind
        except OSError:
            pass
        raise ConfigError(f"could not commit secret (rename failed): {e}")
    _fsync_dir(d)


@contextlib.contextmanager
def lock():
    """Exclusive advisory lock serializing token activation and refresh so a
    read-compare-write cannot interleave with a concurrent reauth (code-r2 #1).
    Unix-only (Windows is out of 0.9 scope)."""
    d = config_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    lock_path = os.path.join(d, ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _fsync_dir(d):
    try:
        dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def remove_secret(name):
    target = _path(name)
    try:
        os.remove(target)
        _fsync_dir(config_dir())
    except FileNotFoundError:
        pass


# --- token envelope: token + scope provenance, one atomic unit (r3 #1) ---

def refresh_token_hash(token_dict):
    """Stable binding: hash the refresh_token only (NOT access token/expiry,
    which rotate on every refresh)."""
    rt = (token_dict or {}).get("refresh_token") or ""
    return hashlib.sha256(rt.encode("utf-8")).hexdigest()


def read_token_envelope():
    """Return the envelope dict {"token":..., "provenance":...} or None."""
    raw = read_secret_bytes(TOKEN_NAME)
    if raw is None:
        return None
    try:
        env = json.loads(raw)
    except ValueError:
        raise ConfigError("token store is corrupt — run `skrepka init`")
    if not isinstance(env, dict) or "token" not in env:
        raise ConfigError("token store is malformed — run `skrepka init`")
    return env


def write_token_envelope(token_dict, provenance):
    env = {"token": token_dict, "provenance": provenance}
    write_secret_bytes(
        TOKEN_NAME, json.dumps(env, ensure_ascii=False).encode("utf-8"))


# --- write-ahead commit marker: barrier for ALL readers (r3 #3) ---

def read_marker():
    raw = read_secret_bytes(MARKER_NAME)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        # a present-but-unreadable marker is still a barrier
        return {"state": "unknown"}


def write_marker(payload):
    write_secret_bytes(
        MARKER_NAME, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def clear_marker():
    remove_secret(MARKER_NAME)


def require_no_pending_init():
    """Barrier used by every non-init reader. A present marker means a prior
    init/reauth crashed mid-activation; the secret set may be half-written,
    so normal commands must fail closed (r3 #3)."""
    if read_marker() is not None:
        raise PendingInitError(
            "a previous `skrepka init` did not finish (commit marker present) "
            "— run `skrepka init` to complete or roll it back before using "
            "other commands")


# --- legacy detection (r2 #11): detect, do NOT auto-migrate (that is R3) ---

def detect_legacy():
    """True when the skrepka dir has no token but the old gdocs-uploader dir
    does — surface a hint; migration itself is R3."""
    try:
        if read_secret_bytes(TOKEN_NAME) is not None:
            return False
    except ConfigError:
        return False
    old = os.path.join(legacy_dir(), TOKEN_NAME)
    return os.path.exists(old)


def credentials_path():
    return _path(CREDENTIALS_NAME)


def token_path():
    return _path(TOKEN_NAME)
