"""Hardened filesystem writer for artifacts in the USER's working directory:
downloaded documents/images, sidecars, and the --output file.

Unlike `config.write_secret_bytes` (app-owned config dir, with owner-chain
checks), these files land wherever the user runs skrepka, so we cannot assume
ownership of the parent chain. The threat (codex R3 #9 / plan r1 #9): a symlink
planted at a PREDICTABLE artifact path. A naive `open(path, "w")` or
`os.makedirs(path)` FOLLOWS such a link and writes the document's contents
through it, overwriting an arbitrary file the user can write — e.g. a
`Title.md` or `.gdocs-base.json.tmp` symlinked onto `~/.ssh/authorized_keys`,
or a predictable `<doc>_images` dir symlinked elsewhere.

Threat model and where we draw the line:
  * The TARGET file and the IMMEDIATE PARENT directory (the dir that will
    directly contain what we write) carry predictable, attacker-guessable
    names — these are the attack surface and are handled with O_NOFOLLOW and
    exclusive creation: a symlink there fails closed.
  * ANCESTOR directories above the immediate parent are the user's existing
    environment. We deliberately FOLLOW symlinks there: it is both normal
    filesystem behaviour and mandatory on macOS, where `/var`, `/tmp` and
    `/etc` are themselves symlinks into `/private`. Refusing every symlinked
    component (the plan's literal wording) would make it impossible to write
    into a temp dir at all. An attacker who can plant a symlink at a *distant
    ancestor* already has write access to the user's directory tree.

Mechanics, TOCTOU-free:
  * Descend the ancestor path with plain openat (follows existing dirs), then
    open the immediate parent with O_NOFOLLOW (creating it 0700 with exclusive
    mkdir when create=True) so a symlink at that predictable name is refused.
  * Do ALL file work relative to that verified parent fd — no check/use gap.
  * Never open the final path with O_CREAT|O_WRONLY (that follows a final
    symlink). Write an unpredictable O_EXCL|O_NOFOLLOW temp in the parent,
    fsync, then atomically rename it over the target (rename REPLACES a final
    symlink rather than writing through it), then fsync the directory. An
    existing non-regular target is refused loudly first.

Unix-only (Windows is out of 0.9 scope), matching config.py.
"""

import errno
import os
import secrets
import stat


class SafeIOError(Exception):
    """A write was refused (symlinked immediate parent/target, non-regular
    target, missing directory) or could not be committed. Fail closed — never
    write through."""


def _split_abs(path):
    """Absolute, lexically-normalized path + its components (leading '/'
    excluded). No symlink resolution — we refuse symlinks at the sensitive
    positions and follow them elsewhere, so `realpath` would be wrong here."""
    ap = os.path.abspath(path)
    parts = []
    head, tail = os.path.split(ap)
    while tail:
        parts.append(tail)
        head, tail = os.path.split(head)
    parts.reverse()
    return ap, parts


def _descend_ancestor(parent_fd, comp, ap, create, dir_mode, creating):
    """Descend one ancestor directory component. While the existing prefix
    lasts, FOLLOW symlinks (the user's environment, incl. macOS /var). Once a
    component is missing (create=True), switch to creating the rest with
    exclusive mkdir + O_NOFOLLOW so an attacker cannot pre-plant a symlink at a
    directory WE create. Returns (fd, creating)."""
    if comp in (".", ".."):
        raise SafeIOError(f"unexpected path component {comp!r} in {ap}")
    if creating:
        return _open_leaf_dir(parent_fd, comp, True, dir_mode, ap), True
    try:
        return os.open(comp, os.O_RDONLY | os.O_DIRECTORY,
                       dir_fd=parent_fd), False
    except OSError as e:
        if e.errno == errno.ENOENT:
            if not create:
                raise SafeIOError(
                    f"directory does not exist: {os.path.dirname(ap)}")
            return _open_leaf_dir(parent_fd, comp, True, dir_mode, ap), True
        if e.errno == errno.ENOTDIR:
            raise SafeIOError(
                f"refusing to write: {comp!r} in the path is not a directory "
                f"({ap})")
        raise


def _open_leaf_dir(parent_fd, leaf, create, dir_mode, ap):
    """Open the IMMEDIATE parent directory `leaf` with O_NOFOLLOW so a symlink
    at this predictable name fails closed. With create=True, create it 0700
    (exclusive mkdir) when missing. Returns a directory fd."""
    if leaf in (".", ".."):
        raise SafeIOError(f"unexpected path component {leaf!r} in {ap}")
    try:
        return os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                       dir_fd=parent_fd)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise SafeIOError(
                f"refusing to write: the directory {leaf!r} is a symlink "
                f"({ap}) — fail closed")
        if e.errno == errno.ENOTDIR:
            raise SafeIOError(f"refusing to write: {leaf!r} is not a directory "
                              f"({ap})")
        if e.errno != errno.ENOENT:
            raise
    if not create:
        raise SafeIOError(f"directory does not exist: {os.path.dirname(ap)}")
    try:
        os.mkdir(leaf, dir_mode, dir_fd=parent_fd)
    except FileExistsError:
        pass  # created concurrently; the NOFOLLOW open below rejects a symlink
    try:
        return os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                       dir_fd=parent_fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SafeIOError(
                f"refusing to write: the directory {leaf!r} is a symlink "
                f"({ap}) — fail closed")
        raise


def secure_open_parent(target, *, create=False, dir_mode=0o700):
    """Open the directory that will directly hold `target`. Ancestor dirs are
    followed (existing environment); the immediate parent is opened O_NOFOLLOW
    (created 0700 if create=True) so a symlink at that predictable name fails
    closed. Returns a directory fd the caller MUST os.close()."""
    ap, parts = _split_abs(target)
    pparts = parts[:-1]  # components of the immediate parent directory
    if not pparts:
        raise SafeIOError(f"refusing to write to a filesystem root: {ap}")
    ancestors, leaf = pparts[:-1], pparts[-1]
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)  # root is never a symlink
    creating = False
    try:
        for comp in ancestors:
            nfd, creating = _descend_ancestor(fd, comp, ap, create, dir_mode,
                                              creating)
            os.close(fd)
            fd = nfd
        nfd = _open_leaf_dir(fd, leaf, create, dir_mode, ap)
        os.close(fd)
        return nfd
    except BaseException:
        os.close(fd)
        raise


def write_at(dir_fd, name, data, *, mode=0o600):
    """Atomically write `data` (bytes/str) to `name` inside the already-verified
    directory `dir_fd`. Refuses a non-regular existing target; writes an O_EXCL
    temp, fsyncs, renames over the target, fsyncs the dir. `name` must be a bare
    filename (no path separators)."""
    if os.sep in name or (os.altsep and os.altsep in name) or name in ("", ".",
                                                                        ".."):
        raise SafeIOError(f"write_at name must be a bare filename: {name!r}")
    if isinstance(data, str):
        data = data.encode("utf-8")
    # A pre-existing non-regular target (symlink/dir/device) is refused loudly.
    # (Security does not depend on this check — the atomic rename below replaces
    # a symlink rather than writing through it — but failing early is clearer.)
    try:
        st = os.lstat(name, dir_fd=dir_fd)
        if not stat.S_ISREG(st.st_mode):
            raise SafeIOError(
                f"{name} exists and is not a regular file "
                f"(symlink/dir/device) — refusing to overwrite (fail closed)")
    except FileNotFoundError:
        pass
    tmpname = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmpname,
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 mode, dir_fd=dir_fd)
    try:
        view = memoryview(data)
        written = 0
        while written < len(data):
            n = os.write(fd, view[written:])
            if n <= 0:
                raise SafeIOError("short write")
            written += n
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        _silent_unlink(tmpname, dir_fd)
        raise
    else:
        os.close(fd)
    try:
        os.replace(tmpname, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError as e:
        _silent_unlink(tmpname, dir_fd)
        raise SafeIOError(f"could not commit {name} (rename failed): {e}")
    try:
        os.fsync(dir_fd)
    except OSError:
        pass


def _silent_unlink(name, dir_fd):
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


def atomic_write(path, data, *, mode=0o600, make_parents=False,
                 dir_mode=0o700):
    """Resolve `path`'s immediate parent safely (creating it 0700 when
    make_parents=True), write `data` there atomically, and close the parent fd.
    Returns the absolute path written. `make_parents` creates only the single
    immediate parent directory, not a deep chain."""
    ap, parts = _split_abs(path)
    pfd = secure_open_parent(ap, create=make_parents, dir_mode=dir_mode)
    try:
        write_at(pfd, parts[-1], data, mode=mode)
    finally:
        os.close(pfd)
    return ap
